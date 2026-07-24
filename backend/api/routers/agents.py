"""智能体展示记录与知识库绑定管理 API（CRUD）

这里的数据库记录只控制展示、启停和知识库绑定，不创建运行时智能体。
运行能力必须先由 Agent Profile 及其关联注册表完整声明：
  - GET    /api/v1/agents               — 智能体列表
  - POST   /api/v1/agents               — 注册/更新展示记录
  - GET    /api/v1/agents/{code}        — 智能体详情（含绑定知识库）
  - PUT    /api/v1/agents/{code}/kbs    — 全量设置绑定知识库
  - POST   /api/v1/agents/{code}/kbs   — 追加知识库绑定
  - DELETE /api/v1/agents/{code}/kbs/{kb_id} — 删除某条绑定
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, get_db_session, require_admin
from backend.application.agents.definition import (
    AgentDefinitionValidationError,
    AgentDefinitionValidator,
)
from backend.application.blocking import run_blocking
from backend.services.admin_audit import write_admin_audit

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


async def _load_kb_bindings(
    session: AsyncSession, agent_codes: list[str]
) -> dict[str, list[KbBindingItem]]:
    if not agent_codes:
        return {}
    from backend.db.models.agent import AgentKnowledgeBase
    from backend.db.models.knowledge_base import KnowledgeBase

    rows = await session.execute(
        select(AgentKnowledgeBase, KnowledgeBase.kb_name)
        .join(KnowledgeBase, AgentKnowledgeBase.kb_id == KnowledgeBase.kb_id)
        .where(AgentKnowledgeBase.agent_code.in_(agent_codes))
        .order_by(
            AgentKnowledgeBase.agent_code,
            AgentKnowledgeBase.sort_order,
            AgentKnowledgeBase.id,
        )
    )
    grouped: dict[str, list[KbBindingItem]] = {
        agent_code: [] for agent_code in agent_codes
    }
    for row in rows:
        binding = row.AgentKnowledgeBase
        grouped[binding.agent_code].append(
            KbBindingItem(
                kb_id=binding.kb_id,
                kb_name=row.kb_name,
                sort_order=binding.sort_order,
            )
        )
    return grouped


@router.get("", response_model=List[AgentDetailResponse])
async def list_agents(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    _admin: ApiKeyAuth = Depends(require_admin),
):
    """获取所有智能体及其绑定知识库。"""
    from backend.db.models.agent import Agent

    agents_result = await session.execute(
        select(Agent)
        .order_by(Agent.sort_order, Agent.agent_code)
        .offset(offset)
        .limit(limit)
    )
    agents = list(agents_result.scalars().all())
    bindings = await _load_kb_bindings(
        session, [agent.agent_code for agent in agents]
    )
    return [
        AgentDetailResponse(
            agent_code=agent.agent_code,
            agent_name=agent.agent_name,
            description=agent.description,
            is_active=agent.is_active,
            sort_order=agent.sort_order,
            kb_bindings=bindings.get(agent.agent_code, []),
        )
        for agent in agents
    ]


@router.post("/{agent_code}", response_model=AgentDetailResponse)
async def upsert_agent(
    agent_code: str,
    payload: AgentUpsertRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: ApiKeyAuth = Depends(require_admin),
):
    """注册或更新展示记录；运行时 Profile 必须已通过统一校验。"""
    from backend.db.crud.agent_crud import upsert_agent as _upsert
    try:
        validator = AgentDefinitionValidator()
        await run_blocking(validator.resolve, agent_code)
    except AgentDefinitionValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "运行时 Agent Profile 未注册或配置不完整",
                "validation": exc.report.to_json_dict(),
            },
        ) from exc
    except Exception as exc:  # noqa: BLE001 - admin receives actionable config error
        raise HTTPException(
            status_code=422,
            detail={
                "message": "运行时 Agent Profile 校验失败",
                "error": str(exc),
            },
        ) from exc

    agent = await _upsert(
        session,
        agent_code=agent_code,
        agent_name=payload.agent_name,
        description=payload.description,
        is_active=payload.is_active,
        sort_order=payload.sort_order,
    )
    await write_admin_audit(
        session,
        request,
        admin,
        action="agent.upsert",
        target_type="agent",
        target_id=agent_code,
        detail=payload.model_dump(),
    )
    await session.flush()
    await session.refresh(agent)
    kb_bindings = (await _load_kb_bindings(session, [agent_code]))[agent_code]

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
    session: AsyncSession = Depends(get_db_session),
    _admin: ApiKeyAuth = Depends(require_admin),
):
    """获取智能体详情（含绑定知识库列表）。"""
    from backend.db.models.agent import Agent

    agent = await session.get(Agent, agent_code)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_code}' not found")
    kb_bindings = (await _load_kb_bindings(session, [agent_code]))[agent_code]

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
    payload: SetKbsRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: ApiKeyAuth = Depends(require_admin),
):
    """全量替换智能体的知识库绑定（先清空再插入）。"""
    from backend.db.models.agent import Agent
    from backend.db.crud.agent_crud import set_agent_kb_bindings

    agent = await session.get(Agent, agent_code)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_code}' not found")
    await set_agent_kb_bindings(session, agent_code, payload.kb_ids)
    await write_admin_audit(
        session,
        request,
        admin,
        action="agent.kbs.replace",
        target_type="agent",
        target_id=agent_code,
        detail={"kb_ids": payload.kb_ids},
    )
    await session.flush()
    kb_bindings = (await _load_kb_bindings(session, [agent_code]))[agent_code]

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
    payload: AddKbRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: ApiKeyAuth = Depends(require_admin),
):
    """为智能体追加一条知识库绑定（已存在则更新 sort_order）。"""
    from backend.db.models.agent import Agent
    from backend.db.models.knowledge_base import KnowledgeBase
    from backend.db.crud.agent_crud import add_kb_binding

    agent = await session.get(Agent, agent_code)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_code}' not found")
    kb = await session.get(KnowledgeBase, payload.kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail=f"KnowledgeBase '{payload.kb_id}' not found")
    await add_kb_binding(session, agent_code, payload.kb_id, payload.sort_order)
    await write_admin_audit(
        session,
        request,
        admin,
        action="agent.kb.add",
        target_type="agent",
        target_id=agent_code,
        detail=payload.model_dump(),
    )

    return KbBindingItem(kb_id=payload.kb_id, kb_name=kb.kb_name, sort_order=payload.sort_order)


@router.delete("/{agent_code}/kbs/{kb_id}")
async def remove_agent_kb(
    agent_code: str,
    kb_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: ApiKeyAuth = Depends(require_admin),
):
    """删除智能体的某条知识库绑定。"""
    from backend.db.crud.agent_crud import remove_kb_binding

    deleted = await remove_kb_binding(session, agent_code, kb_id)
    if deleted:
        await write_admin_audit(
            session,
            request,
            admin,
            action="agent.kb.remove",
            target_type="agent",
            target_id=agent_code,
            detail={"kb_id": kb_id},
        )

    if deleted == 0:
        raise HTTPException(status_code=404, detail="Binding not found")
    return {"status": "success", "deleted": deleted}
