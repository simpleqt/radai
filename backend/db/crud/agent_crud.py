"""智能体-知识库绑定 CRUD 操作"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select, delete

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

async def get_agent_kb_ids(session: "AsyncSession", agent_code: str) -> list[str]:
    """返回指定智能体绑定的知识库 ID 列表（按 sort_order 升序）。

    若未在数据库中找到任何绑定，返回空列表（由调用方决定是否使用硬编码默认值）。
    """
    from backend.db.models.agent import AgentKnowledgeBase

    stmt = (
        select(AgentKnowledgeBase.kb_id)
        .where(AgentKnowledgeBase.agent_code == agent_code)
        .order_by(AgentKnowledgeBase.sort_order, AgentKnowledgeBase.id)
    )
    result = await session.execute(stmt)
    return [row.kb_id for row in result]


async def get_agent(session: "AsyncSession", agent_code: str):
    """返回智能体配置记录，若不存在返回 None。"""
    from backend.db.models.agent import Agent

    return await session.get(Agent, agent_code)


async def list_agents(session: "AsyncSession", active_only: bool = False):
    """返回所有智能体配置列表（按 sort_order 升序）。"""
    from backend.db.models.agent import Agent

    stmt = select(Agent).order_by(Agent.sort_order, Agent.agent_code)
    if active_only:
        stmt = stmt.where(Agent.is_active == True)  # noqa: E712
    result = await session.execute(stmt)
    return result.scalars().all()


async def list_agent_bindings(session: "AsyncSession", agent_code: str):
    """返回指定智能体的所有绑定记录（按 sort_order 升序）。"""
    from backend.db.models.agent import AgentKnowledgeBase
    from backend.db.models.knowledge_base import KnowledgeBase

    stmt = (
        select(AgentKnowledgeBase, KnowledgeBase.kb_name)
        .join(KnowledgeBase, AgentKnowledgeBase.kb_id == KnowledgeBase.kb_id)
        .where(AgentKnowledgeBase.agent_code == agent_code)
        .order_by(AgentKnowledgeBase.sort_order, AgentKnowledgeBase.id)
    )
    result = await session.execute(stmt)
    return result.all()


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

async def upsert_agent(
    session: "AsyncSession",
    agent_code: str,
    agent_name: str,
    description: str | None = None,
    is_active: bool = True,
    sort_order: int = 0,
):
    """创建或更新智能体配置记录。"""
    from backend.db.models.agent import Agent

    agent = await session.get(Agent, agent_code)
    if agent is None:
        agent = Agent(
            agent_code=agent_code,
            agent_name=agent_name,
            description=description,
            is_active=is_active,
            sort_order=sort_order,
        )
        session.add(agent)
    else:
        agent.agent_name = agent_name
        if description is not None:
            agent.description = description
        agent.is_active = is_active
        agent.sort_order = sort_order
    await session.flush()
    return agent


async def add_kb_binding(
    session: "AsyncSession",
    agent_code: str,
    kb_id: str,
    sort_order: int = 0,
):
    """为智能体添加知识库绑定（已存在则忽略）。"""
    from backend.db.models.agent import AgentKnowledgeBase
    from sqlalchemy.dialects.mysql import insert as mysql_insert

    stmt = (
        mysql_insert(AgentKnowledgeBase)
        .values(agent_code=agent_code, kb_id=kb_id, sort_order=sort_order)
        .on_duplicate_key_update(sort_order=sort_order)
    )
    await session.execute(stmt)


async def remove_kb_binding(
    session: "AsyncSession",
    agent_code: str,
    kb_id: str,
) -> int:
    """删除智能体的某条知识库绑定，返回删除行数。"""
    from backend.db.models.agent import AgentKnowledgeBase

    stmt = delete(AgentKnowledgeBase).where(
        AgentKnowledgeBase.agent_code == agent_code,
        AgentKnowledgeBase.kb_id == kb_id,
    )
    result = await session.execute(stmt)
    return result.rowcount


async def set_agent_kb_bindings(
    session: "AsyncSession",
    agent_code: str,
    kb_ids: list[str],
) -> None:
    """全量替换智能体的知识库绑定（先删后插）。"""
    from backend.db.models.agent import AgentKnowledgeBase

    await session.execute(
        delete(AgentKnowledgeBase).where(
            AgentKnowledgeBase.agent_code == agent_code
        )
    )
    for idx, kb_id in enumerate(kb_ids):
        session.add(
            AgentKnowledgeBase(
                agent_code=agent_code,
                kb_id=kb_id,
                sort_order=idx,
            )
        )
    await session.flush()
