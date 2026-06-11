"""Frontend-compatible agent information service."""

from __future__ import annotations

import logging
from typing import Any

from backend.domain.agents.profile import AgentProfile

logger = logging.getLogger(__name__)


class AgentInfoService:
    """Builds AgentInfo responses from platform profiles and shared config."""

    async def get_info(self, profile: AgentProfile) -> dict[str, Any]:
        kb_ids = await self._get_agent_default_kb_ids(profile)
        available_kbs = await self._fetch_kb_options(kb_ids)

        from backend.core.agents.config_compat import (
            get_available_model_info,
            get_default_model_name,
        )

        default_model = profile.default_model or get_default_model_name()
        return {
            "name": profile.name,
            "model": default_model,
            "default_model": default_model,
            "available_models": get_available_model_info(),
            "kb_ids": kb_ids,
            "available_kbs": available_kbs,
            "subagent_enabled": False,
            "search_enabled": True,
            "task_type": None,
            "max_concurrent_subagents": 3,
            "selected_skills": sorted(profile.default_skills),
            "lightweight_mode": False,
            "lightweight_has_kb": False,
            "features": self._features(kb_ids=kb_ids, use_search=True, subagent_enabled=False),
        }

    async def _get_agent_default_kb_ids(self, profile: AgentProfile) -> list[str]:
        try:
            from backend.db.connection import get_async_session_factory
            from backend.db.crud.agent_crud import get_agent_kb_ids

            session_factory = get_async_session_factory()
            async with session_factory() as session:
                db_ids = await get_agent_kb_ids(session, self._kb_binding_agent_code(profile))
            if db_ids:
                return list(db_ids)
        except Exception as exc:
            logger.warning(
                "[AgentInfoService] 读取智能体 %s 知识库绑定失败，使用 profile 默认值: %s",
                profile.code,
                exc,
            )
        return list(profile.default_kb_ids)

    async def _fetch_kb_options(self, kb_ids: list[str]) -> list[dict[str, str]]:
        if not kb_ids:
            return []
        try:
            from sqlalchemy import select

            from backend.db.connection import get_async_session_factory
            from backend.db.models.knowledge_base import KnowledgeBase

            session_factory = get_async_session_factory()
            async with session_factory() as session:
                stmt = select(KnowledgeBase.kb_id, KnowledgeBase.kb_name).where(
                    KnowledgeBase.kb_id.in_(kb_ids)
                )
                result = await session.execute(stmt)
                rows = {row.kb_id: row.kb_name for row in result}
            return [{"id": kb_id, "label": rows.get(kb_id) or kb_id} for kb_id in kb_ids]
        except Exception as exc:
            logger.warning("[AgentInfoService] 查询知识库名称失败，降级使用 ID: %s", exc)
            return [{"id": kb_id, "label": kb_id} for kb_id in kb_ids]

    def _features(
        self,
        *,
        kb_ids: list[str],
        use_search: bool,
        subagent_enabled: bool,
    ) -> list[str]:
        features = [
            "guardrail",
            "context_summarization",
            "vision",
            "file_presentation",
            "deep_research_report",
        ]
        if kb_ids:
            features.append("multi_kb_search")
        if use_search:
            features.append("web_search")
        if subagent_enabled:
            features.append("task_delegation")
        return features

    def _kb_binding_agent_code(self, profile: AgentProfile) -> str:
        if profile.code == "csh":
            return "csh_full_feature"
        if profile.code == "ne":
            return "ne_full_feature"
        return profile.code
