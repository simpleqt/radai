"""智能体展示记录与知识库绑定管理 API（CRUD）

这里的数据库记录只控制展示、启停和知识库绑定，不创建运行时智能体。
运行能力必须先由 Agent Profile 及其关联注册表完整声明：
  - GET    /api/v1/agents               — 智能体列表（按 Profile 合并展示记录）
  - POST   /api/v1/agents/{code}        — 注册/更新展示记录（旧编码会写入运行时编码）
  - GET    /api/v1/agents/{code}        — 智能体详情（含绑定知识库）
  - PUT    /api/v1/agents/{code}/kbs    — 全量设置绑定知识库
  - POST   /api/v1/agents/{code}/kbs   — 追加知识库绑定
  - DELETE /api/v1/agents/{code}/kbs/{kb_id} — 删除某条绑定
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, get_db_session, require_admin
from backend.application.agents.code_resolver import AgentCodeResolver
from backend.application.agents.definition import (
    AgentDefinitionValidationError,
    AgentDefinitionValidator,
)
from backend.application.agents.profile_loader import AgentProfileLoader
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
    has_display_record: bool = True
    legacy_agent_code: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class SetKbsRequest(BaseModel):
    # 与 explore 系列请求的 kb_ids 上界保持一致，防止两端漂移。
    kb_ids: List[str] = Field(default_factory=list, max_length=50)


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


async def _resolve_profile_or_422(agent_code: str):
    runtime_code = AgentCodeResolver.to_runtime(agent_code)
    try:
        validator = AgentDefinitionValidator()
        definition = await run_blocking(validator.resolve, runtime_code)
        return definition.profile.code
    except AgentDefinitionValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "运行时 Agent Profile 未注册或配置不完整",
                "runtime_agent_code": runtime_code,
                "validation": exc.report.to_json_dict(),
            },
        ) from exc
    except Exception as exc:  # noqa: BLE001 - admin receives actionable config error
        raise HTTPException(
            status_code=422,
            detail={
                "message": "运行时 Agent Profile 校验失败",
                "runtime_agent_code": runtime_code,
                "error": str(exc),
            },
        ) from exc


async def _get_display_agent(session: AsyncSession, agent_code: str):
    """Load display row by runtime code, falling back to legacy alias."""
    from backend.db.models.agent import Agent

    runtime_code = AgentCodeResolver.to_runtime(agent_code)
    agent = await session.get(Agent, runtime_code)
    if agent is not None:
        return agent, runtime_code, None

    legacy = AgentCodeResolver.legacy_alias(runtime_code)
    if legacy:
        legacy_agent = await session.get(Agent, legacy)
        if legacy_agent is not None:
            return legacy_agent, runtime_code, legacy
    return None, runtime_code, legacy


def _pick_kb_bindings(
    grouped: dict[str, list[KbBindingItem]],
    *,
    runtime_code: str,
    legacy_code: str | None,
) -> list[KbBindingItem]:
    if grouped.get(runtime_code):
        return grouped[runtime_code]
    if legacy_code and grouped.get(legacy_code):
        return grouped[legacy_code]
    return []


@router.get("", response_model=List[AgentDetailResponse])
async def list_agents(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    _admin: ApiKeyAuth = Depends(require_admin),
):
    """按已部署 Profile 列出智能体，并合并数据库展示记录 / 旧编码绑定。"""
    from backend.db.models.agent import Agent

    profiles = await run_blocking(AgentProfileLoader().list_profiles)
    agents_result = await session.execute(select(Agent))
    db_by_code = {
        str(agent.agent_code): agent for agent in agents_result.scalars().all()
    }

    lookup_codes: list[str] = []
    for profile in profiles:
        runtime = str(profile.code)
        lookup_codes.append(runtime)
        legacy = AgentCodeResolver.legacy_alias(runtime)
        if legacy:
            lookup_codes.append(legacy)
    for code in db_by_code:
        if code not in lookup_codes:
            lookup_codes.append(code)
    bindings = await _load_kb_bindings(session, lookup_codes)

    items: list[AgentDetailResponse] = []
    covered: set[str] = set()

    for index, profile in enumerate(profiles):
        runtime = str(profile.code)
        legacy = AgentCodeResolver.legacy_alias(runtime)
        primary = db_by_code.get(runtime)
        legacy_row = db_by_code.get(legacy) if legacy else None
        source = primary or legacy_row
        if primary is not None:
            covered.add(runtime)
        if legacy_row is not None and legacy:
            covered.add(legacy)

        kb_bindings = _pick_kb_bindings(
            bindings, runtime_code=runtime, legacy_code=legacy
        )
        if source is not None:
            items.append(
                AgentDetailResponse(
                    agent_code=runtime,
                    agent_name=source.agent_name,
                    description=source.description,
                    is_active=bool(source.is_active),
                    sort_order=int(source.sort_order),
                    kb_bindings=kb_bindings,
                    has_display_record=primary is not None,
                    legacy_agent_code=(
                        legacy if legacy_row is not None and primary is None else None
                    ),
                )
            )
        else:
            items.append(
                AgentDetailResponse(
                    agent_code=runtime,
                    agent_name=profile.name,
                    description=profile.description,
                    is_active=False,
                    sort_order=1000 + index,
                    kb_bindings=[],
                    has_display_record=False,
                    legacy_agent_code=None,
                )
            )

    for code, agent in sorted(
        db_by_code.items(), key=lambda item: (item[1].sort_order, item[0])
    ):
        if code in covered:
            continue
        runtime = AgentCodeResolver.to_runtime(code)
        # Orphan display rows that do not map to a deployed profile.
        items.append(
            AgentDetailResponse(
                agent_code=code,
                agent_name=agent.agent_name,
                description=agent.description,
                is_active=bool(agent.is_active),
                sort_order=int(agent.sort_order),
                kb_bindings=bindings.get(code, []),
                has_display_record=True,
                legacy_agent_code=code if runtime != code else None,
            )
        )

    items.sort(key=lambda item: (item.sort_order, item.agent_code))
    return items[offset : offset + limit]


@router.post("/{agent_code}", response_model=AgentDetailResponse)
async def upsert_agent(
    agent_code: str,
    payload: AgentUpsertRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: ApiKeyAuth = Depends(require_admin),
):
    """注册或更新展示记录；旧编码会规范化为运行时 Profile 编码后写入。"""
    from backend.db.crud.agent_crud import upsert_agent as _upsert
    from backend.db.models.agent import Agent

    runtime_code = await _resolve_profile_or_422(agent_code)
    legacy = AgentCodeResolver.legacy_alias(runtime_code)
    legacy_row = await session.get(Agent, legacy) if legacy else None

    agent = await _upsert(
        session,
        agent_code=runtime_code,
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
        target_id=runtime_code,
        detail={
            **payload.model_dump(),
            "requested_agent_code": agent_code,
            "runtime_agent_code": runtime_code,
            "migrated_from_legacy": bool(
                legacy and AgentCodeResolver.to_runtime(agent_code) == runtime_code
                and agent_code.strip().lower() != runtime_code
            ),
        },
    )
    await session.flush()
    await session.refresh(agent)
    kb_bindings = _pick_kb_bindings(
        await _load_kb_bindings(
            session, [runtime_code] + ([legacy] if legacy else [])
        ),
        runtime_code=runtime_code,
        legacy_code=legacy,
    )

    return AgentDetailResponse(
        agent_code=agent.agent_code,
        agent_name=agent.agent_name,
        description=agent.description,
        is_active=agent.is_active,
        sort_order=agent.sort_order,
        kb_bindings=kb_bindings,
        has_display_record=True,
        legacy_agent_code=legacy if legacy_row is not None else None,
    )


@router.get("/{agent_code}", response_model=AgentDetailResponse)
async def get_agent(
    agent_code: str,
    session: AsyncSession = Depends(get_db_session),
    _admin: ApiKeyAuth = Depends(require_admin),
):
    """获取智能体详情（含绑定知识库列表；兼容旧编码）。"""
    agent, runtime_code, legacy = await _get_display_agent(session, agent_code)
    if agent is None:
        raise HTTPException(
            status_code=404, detail=f"Agent '{agent_code}' not found"
        )

    codes = [runtime_code] + ([legacy] if legacy else [])
    kb_bindings = _pick_kb_bindings(
        await _load_kb_bindings(session, codes),
        runtime_code=runtime_code,
        legacy_code=legacy,
    )
    primary = agent.agent_code == runtime_code
    return AgentDetailResponse(
        agent_code=runtime_code,
        agent_name=agent.agent_name,
        description=agent.description,
        is_active=agent.is_active,
        sort_order=agent.sort_order,
        kb_bindings=kb_bindings,
        has_display_record=primary,
        legacy_agent_code=None if primary else agent.agent_code,
    )


@router.put("/{agent_code}/kbs", response_model=AgentDetailResponse)
async def set_agent_kbs(
    agent_code: str,
    payload: SetKbsRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: ApiKeyAuth = Depends(require_admin),
):
    """全量替换智能体的知识库绑定（写入运行时编码；兼容旧编码路径）。"""
    from backend.db.crud.agent_crud import set_agent_kb_bindings, upsert_agent as _upsert
    from backend.db.models.agent import Agent

    runtime_code = AgentCodeResolver.to_runtime(agent_code)
    agent = await session.get(Agent, runtime_code)
    legacy = AgentCodeResolver.legacy_alias(runtime_code)
    legacy_row = await session.get(Agent, legacy) if legacy else None
    if agent is None and legacy_row is not None:
        agent = await _upsert(
            session,
            agent_code=runtime_code,
            agent_name=legacy_row.agent_name,
            description=legacy_row.description,
            is_active=legacy_row.is_active,
            sort_order=legacy_row.sort_order,
        )
    if agent is None:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{runtime_code}' not found；请先注册展示记录",
        )

    await set_agent_kb_bindings(session, runtime_code, payload.kb_ids)
    await write_admin_audit(
        session,
        request,
        admin,
        action="agent.kbs.replace",
        target_type="agent",
        target_id=runtime_code,
        detail={
            "kb_ids": payload.kb_ids,
            "requested_agent_code": agent_code,
            "runtime_agent_code": runtime_code,
        },
    )
    await session.flush()
    await session.refresh(agent)
    kb_bindings = (await _load_kb_bindings(session, [runtime_code]))[runtime_code]

    return AgentDetailResponse(
        agent_code=agent.agent_code,
        agent_name=agent.agent_name,
        description=agent.description,
        is_active=agent.is_active,
        sort_order=agent.sort_order,
        kb_bindings=kb_bindings,
        has_display_record=True,
        legacy_agent_code=legacy if legacy_row is not None else None,
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
    from backend.db.crud.agent_crud import add_kb_binding, upsert_agent as _upsert

    runtime_code = AgentCodeResolver.to_runtime(agent_code)
    agent = await session.get(Agent, runtime_code)
    legacy = AgentCodeResolver.legacy_alias(runtime_code)
    legacy_row = await session.get(Agent, legacy) if legacy else None
    if agent is None and legacy_row is not None:
        agent = await _upsert(
            session,
            agent_code=runtime_code,
            agent_name=legacy_row.agent_name,
            description=legacy_row.description,
            is_active=legacy_row.is_active,
            sort_order=legacy_row.sort_order,
        )
    if agent is None:
        raise HTTPException(
            status_code=404, detail=f"Agent '{runtime_code}' not found"
        )
    kb = await session.get(KnowledgeBase, payload.kb_id)
    if not kb:
        raise HTTPException(
            status_code=404, detail=f"KnowledgeBase '{payload.kb_id}' not found"
        )
    await add_kb_binding(session, runtime_code, payload.kb_id, payload.sort_order)
    await write_admin_audit(
        session,
        request,
        admin,
        action="agent.kb.add",
        target_type="agent",
        target_id=runtime_code,
        detail={**payload.model_dump(), "requested_agent_code": agent_code},
    )

    return KbBindingItem(
        kb_id=payload.kb_id, kb_name=kb.kb_name, sort_order=payload.sort_order
    )


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

    runtime_code = AgentCodeResolver.to_runtime(agent_code)
    deleted = await remove_kb_binding(session, runtime_code, kb_id)
    if deleted == 0:
        legacy = AgentCodeResolver.legacy_alias(runtime_code)
        if legacy:
            deleted = await remove_kb_binding(session, legacy, kb_id)
            if deleted:
                runtime_code = legacy
    if deleted:
        await write_admin_audit(
            session,
            request,
            admin,
            action="agent.kb.remove",
            target_type="agent",
            target_id=runtime_code,
            detail={"kb_id": kb_id, "requested_agent_code": agent_code},
        )

    if deleted == 0:
        raise HTTPException(status_code=404, detail="Binding not found")
    return {"status": "success", "deleted": deleted}
