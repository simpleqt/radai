"""智能体管理 API（CRUD）

提供智能体配置及其知识库绑定的管理接口：
  - GET    /api/v1/agents               — 智能体列表
  - POST   /api/v1/agents               — 创建/更新智能体
  - GET    /api/v1/agents/{code}        — 智能体详情（含绑定知识库）
  - PUT    /api/v1/agents/{code}/kbs    — 全量设置绑定知识库
  - POST   /api/v1/agents/{code}/kbs   — 追加知识库绑定
  - DELETE /api/v1/agents/{code}/kbs/{kb_id} — 删除某条绑定
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from backend.api.dependencies.auth import ApiKeyAuth, require_admin
from backend.db.connection import get_async_session_factory

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response 模型
# ---------------------------------------------------------------------------

class AgentUpsertRequest(BaseModel):
    agent_name: str
    description: Optional[str] = None
    is_active: bool = True
    sort_order: int = 0


class KbBindingItem(BaseModel):
    kb_id: str
    kb_name: Optional[str] = None
    sort_order: int = 0


class AgentDetailResponse(BaseModel):
    agent_code: str
    agent_name: str
    description: Optional[str] = None
    is_active: bool
    sort_order: int
    kb_bindings: List[KbBindingItem] = []

    model_config = ConfigDict(from_attributes=True)


class SetKbsRequest(BaseModel):
    kb_ids: List[str]


class AddKbRequest(BaseModel):
    kb_id: str
    sort_order: int = 0


# ---------------------------------------------------------------------------
# 路由实现
# ---------------------------------------------------------------------------

@router.get("", response_model=List[AgentDetailResponse])
async def list_agents(admin: ApiKeyAuth = Depends(require_admin)):
    """获取所有智能体及其绑定知识库。"""
    from backend.db.models.agent import Agent, AgentKnowledgeBase
    from backend.db.models.knowledge_base import KnowledgeBase

    session_factory = get_async_session_factory()
    async with session_factory() as session:
        agents_result = await session.execute(
            select(Agent).order_by(Agent.sort_order, Agent.agent_code)
        )
        agents = agents_result.scalars().all()

        result = []
        for agent in agents:
            bindings_result = await session.execute(
                select(AgentKnowledgeBase, KnowledgeBase.kb_name)
                .join(KnowledgeBase, AgentKnowledgeBase.kb_id == KnowledgeBase.kb_id)
                .where(AgentKnowledgeBase.agent_code == agent.agent_code)
                .order_by(AgentKnowledgeBase.sort_order, AgentKnowledgeBase.id)
            )
            kb_bindings = [
                KbBindingItem(kb_id=row.AgentKnowledgeBase.kb_id, kb_name=row.kb_name, sort_order=row.AgentKnowledgeBase.sort_order)
                for row in bindings_result
            ]
            result.append(
                AgentDetailResponse(
                    agent_code=agent.agent_code,
                    agent_name=agent.agent_name,
                    description=agent.description,
                    is_active=agent.is_active,
                    sort_order=agent.sort_order,
                    kb_bindings=kb_bindings,
                )
            )
    return result


@router.post("/{agent_code}", response_model=AgentDetailResponse)
async def upsert_agent(
    agent_code: str,
    request: AgentUpsertRequest,
    admin: ApiKeyAuth = Depends(require_admin),
):
    """创建或更新智能体配置（不影响知识库绑定）。"""
    from backend.db.crud.agent_crud import upsert_agent as _upsert
    from backend.db.models.agent import AgentKnowledgeBase
    from backend.db.models.knowledge_base import KnowledgeBase

    session_factory = get_async_session_factory()
    async with session_factory() as session:
        agent = await _upsert(
            session,
            agent_code=agent_code,
            agent_name=request.agent_name,
            description=request.description,
            is_active=request.is_active,
            sort_order=request.sort_order,
        )
        await session.commit()
        await session.refresh(agent)

        bindings_result = await session.execute(
            select(AgentKnowledgeBase, KnowledgeBase.kb_name)
            .join(KnowledgeBase, AgentKnowledgeBase.kb_id == KnowledgeBase.kb_id)
            .where(AgentKnowledgeBase.agent_code == agent_code)
            .order_by(AgentKnowledgeBase.sort_order, AgentKnowledgeBase.id)
        )
        kb_bindings = [
            KbBindingItem(kb_id=row.AgentKnowledgeBase.kb_id, kb_name=row.kb_name, sort_order=row.AgentKnowledgeBase.sort_order)
            for row in bindings_result
        ]

    return AgentDetailResponse(
        agent_code=agent.agent_code,
        agent_name=agent.agent_name,
        description=agent.description,
        is_active=agent.is_active,
        sort_order=agent.sort_order,
        kb_bindings=kb_bindings,
    )


@router.get("/{agent_code}", response_model=AgentDetailResponse)
async def get_agent(
    agent_code: str,
    admin: ApiKeyAuth = Depends(require_admin),
):
    """获取智能体详情（含绑定知识库列表）。"""
    from backend.db.models.agent import Agent, AgentKnowledgeBase
    from backend.db.models.knowledge_base import KnowledgeBase

    session_factory = get_async_session_factory()
    async with session_factory() as session:
        agent = await session.get(Agent, agent_code)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_code}' not found")

        bindings_result = await session.execute(
            select(AgentKnowledgeBase, KnowledgeBase.kb_name)
            .join(KnowledgeBase, AgentKnowledgeBase.kb_id == KnowledgeBase.kb_id)
            .where(AgentKnowledgeBase.agent_code == agent_code)
            .order_by(AgentKnowledgeBase.sort_order, AgentKnowledgeBase.id)
        )
        kb_bindings = [
            KbBindingItem(kb_id=row.AgentKnowledgeBase.kb_id, kb_name=row.kb_name, sort_order=row.AgentKnowledgeBase.sort_order)
            for row in bindings_result
        ]

    return AgentDetailResponse(
        agent_code=agent.agent_code,
        agent_name=agent.agent_name,
        description=agent.description,
        is_active=agent.is_active,
        sort_order=agent.sort_order,
        kb_bindings=kb_bindings,
    )


@router.put("/{agent_code}/kbs", response_model=AgentDetailResponse)
async def set_agent_kbs(
    agent_code: str,
    request: SetKbsRequest,
    admin: ApiKeyAuth = Depends(require_admin),
):
    """全量替换智能体的知识库绑定（先清空再插入）。"""
    from backend.db.models.agent import Agent, AgentKnowledgeBase
    from backend.db.models.knowledge_base import KnowledgeBase
    from backend.db.crud.agent_crud import set_agent_kb_bindings

    session_factory = get_async_session_factory()
    async with session_factory() as session:
        agent = await session.get(Agent, agent_code)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_code}' not found")

        await set_agent_kb_bindings(session, agent_code, request.kb_ids)
        await session.commit()

        bindings_result = await session.execute(
            select(AgentKnowledgeBase, KnowledgeBase.kb_name)
            .join(KnowledgeBase, AgentKnowledgeBase.kb_id == KnowledgeBase.kb_id)
            .where(AgentKnowledgeBase.agent_code == agent_code)
            .order_by(AgentKnowledgeBase.sort_order, AgentKnowledgeBase.id)
        )
        kb_bindings = [
            KbBindingItem(kb_id=row.AgentKnowledgeBase.kb_id, kb_name=row.kb_name, sort_order=row.AgentKnowledgeBase.sort_order)
            for row in bindings_result
        ]

    return AgentDetailResponse(
        agent_code=agent.agent_code,
        agent_name=agent.agent_name,
        description=agent.description,
        is_active=agent.is_active,
        sort_order=agent.sort_order,
        kb_bindings=kb_bindings,
    )


@router.post("/{agent_code}/kbs", response_model=KbBindingItem)
async def add_agent_kb(
    agent_code: str,
    request: AddKbRequest,
    admin: ApiKeyAuth = Depends(require_admin),
):
    """为智能体追加一条知识库绑定（已存在则更新 sort_order）。"""
    from backend.db.models.agent import Agent
    from backend.db.models.knowledge_base import KnowledgeBase
    from backend.db.crud.agent_crud import add_kb_binding

    session_factory = get_async_session_factory()
    async with session_factory() as session:
        agent = await session.get(Agent, agent_code)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_code}' not found")

        kb = await session.get(KnowledgeBase, request.kb_id)
        if not kb:
            raise HTTPException(status_code=404, detail=f"KnowledgeBase '{request.kb_id}' not found")

        await add_kb_binding(session, agent_code, request.kb_id, request.sort_order)
        await session.commit()

    return KbBindingItem(kb_id=request.kb_id, kb_name=kb.kb_name, sort_order=request.sort_order)


@router.delete("/{agent_code}/kbs/{kb_id}")
async def remove_agent_kb(
    agent_code: str,
    kb_id: str,
    admin: ApiKeyAuth = Depends(require_admin),
):
    """删除智能体的某条知识库绑定。"""
    from backend.db.crud.agent_crud import remove_kb_binding

    session_factory = get_async_session_factory()
    async with session_factory() as session:
        deleted = await remove_kb_binding(session, agent_code, kb_id)
        await session.commit()

    if deleted == 0:
        raise HTTPException(status_code=404, detail="Binding not found")
    return {"status": "success", "deleted": deleted}
