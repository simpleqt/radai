"""Frontend-compatible agent information service."""

from __future__ import annotations

import logging
from typing import Any

from backend.domain.agents.profile import AgentProfile

logger = logging.getLogger(__name__)


class AgentInfoService:
    """Builds AgentInfo responses from platform profiles and shared config."""

    async def get_info(
        self,
        profile: AgentProfile,
        *,
        user_id: str = "",
        role: str = "user",
    ) -> dict[str, Any]:
        kb_policy = str(profile.kb_policy or "bound_or_selected").strip().lower()
        bound_kb_ids = (
            []
            if kb_policy in {"disabled", "none"}
            else await self._get_agent_default_kb_ids(profile)
        )
        kb_ids = await self._filter_accessible_kb_ids(
            bound_kb_ids,
            user_id=user_id,
            role=role,
        )
        available_kbs = await self._fetch_kb_options(kb_ids)
        selected_skills = sorted(profile.default_skills)
        available_skills: list[dict[str, Any]] = []
        required_skills: list[str] = []
        skill_catalog_status = "ok"
        skill_catalog_error: dict[str, str] | None = None
        try:
            from backend.application.skills.runtime_resolver import SkillRuntimeResolver
            from backend.db.connection import get_async_session_factory

            factory = get_async_session_factory()
            async with factory() as session:
                resolution = await SkillRuntimeResolver().resolve(
                    session,
                    user_id=user_id,
                    role=role,
                    agent_code=profile.code,
                    agent_domain=profile.domain,
                    workflow=None,
                    requested_skills=None,
                    fallback_skills=list(profile.default_skills),
                )
            selected_skills = list(resolution.selected_skills)
            if resolution.managed:
                available_skills = [
                    {
                        "id": item.skill_id,
                        "name": item.name,
                        "label": item.label,
                        "description": item.description,
                        "version": item.version,
                        "required": item.required,
                        "default_selected": item.default_selected,
                        "selectable": item.selectable,
                        "allowed_workflows": item.allowed_workflows,
                        "preferred_workflow": item.preferred_workflow,
                        "guest_selectable": item.guest_selectable,
                    }
                    for item in resolution.available_skills
                ]
                required_skills = [
                    item.name for item in resolution.available_skills if item.required
                ]
            else:
                from backend.application.skills.public_catalog import (
                    PublicSkillCatalog,
                )

                available_skills = PublicSkillCatalog().to_info_options(profile)
                required_skills = [
                    str(item["name"])
                    for item in available_skills
                    if item["required"]
                ]
        except Exception:
            # Keep built-in profile Skills usable, but make the degradation
            # explicit so clients and operators do not mistake an outage for
            # an authoritative empty managed catalog.
            logger.exception("[AgentInfoService] Skill binding lookup failed")
            from backend.application.skills.public_catalog import PublicSkillCatalog

            available_skills = PublicSkillCatalog().to_info_options(profile)
            required_skills = [
                str(item["name"])
                for item in available_skills
                if item["required"]
            ]
            selected_skills = sorted(profile.default_skills)
            skill_catalog_status = "degraded"
            skill_catalog_error = {
                "code": "SKILL_CATALOG_UNAVAILABLE",
                "message": "托管 Skill 目录暂时不可用，当前展示内置配置",
            }

        from backend.core.agents.config_compat import (
            get_available_model_info,
            get_default_model_name,
        )
        from backend.domain.agents.workflows import (
            LIGHTWEIGHT_WORKFLOW,
            SUBAGENT_WORKFLOWS,
            normalize_workflow_name,
        )

        default_model = profile.default_model or get_default_model_name()
        available_models = get_available_model_info()
        default_workflow = normalize_workflow_name(profile.default_workflow)
        allowed_workflows = {
            normalize_workflow_name(item) for item in profile.allowed_workflows
        }
        project_delegation_enabled = profile.domain == "groundwater-project-analysis"
        subagent_enabled = (
            default_workflow in SUBAGENT_WORKFLOWS or project_delegation_enabled
        )
        supports_delegation = (
            bool(allowed_workflows & SUBAGENT_WORKFLOWS) or project_delegation_enabled
        )
        search_enabled = self._supports_tool(profile, "web_search")
        lightweight_mode = default_workflow == LIGHTWEIGHT_WORKFLOW
        return {
            "agent_code": profile.code,
            "name": profile.name,
            "ui_profile": profile.ui_profile,
            "model": default_model,
            "default_model": default_model,
            "available_models": available_models,
            "kb_ids": kb_ids,
            "available_kbs": available_kbs,
            "subagent_enabled": subagent_enabled,
            "search_enabled": search_enabled,
            "search_provider": "firecrawl" if search_enabled else None,
            "default_workflow": default_workflow,
            "allowed_workflows": [
                normalize_workflow_name(workflow)
                for workflow in profile.allowed_workflows
            ],
            # Compatibility alias used by older frontend builds.
            "task_type": default_workflow,
            "max_concurrent_subagents": 5,
            "selected_skills": selected_skills,
            "required_skills": required_skills,
            "available_skills": available_skills,
            "skill_catalog_status": skill_catalog_status,
            "skill_catalog_error": skill_catalog_error,
            "lightweight_mode": lightweight_mode,
            "lightweight_has_kb": lightweight_mode and bool(kb_ids),
            "features": self._features(
                profile=profile,
                kb_ids=kb_ids,
                available_models=available_models,
                use_search=search_enabled,
                supports_delegation=supports_delegation,
            ),
        }

    @staticmethod
    def _supports_tool(profile: AgentProfile, tool_name: str) -> bool:
        try:
            from backend.application.tools.assembler import (
                shared_default_tool_registry,
            )

            registry = shared_default_tool_registry()
            selected_skills = set(profile.default_skills)
            for workflow in profile.allowed_workflows:
                specs = registry.resolve_policy_tools(
                    policy_name=profile.tool_policy,
                    domain=profile.domain,
                    workflow=workflow,
                    selected_skills=selected_skills,
                )
                if any(spec.name == tool_name for spec in specs):
                    return True
            return False
        except (OSError, ValueError):
            return False

    async def _get_agent_default_kb_ids(self, profile: AgentProfile) -> list[str]:
        try:
            from backend.db.connection import get_async_session_factory
            from backend.db.crud.agent_crud import get_runtime_agent_kb_ids

            session_factory = get_async_session_factory()
            async with session_factory() as session:
                db_ids = await get_runtime_agent_kb_ids(session, profile.code)
            return list(db_ids)
        except Exception as exc:
            logger.warning(
                "[AgentInfoService] 读取智能体 %s 的 MySQL 知识库绑定失败: %s",
                profile.code,
                exc,
            )
        return []

    async def _filter_accessible_kb_ids(
        self,
        kb_ids: list[str],
        *,
        user_id: str,
        role: str,
    ) -> list[str]:
        if not kb_ids:
            return []
        try:
            from backend.api.dependencies.auth import (
                ApiKeyAuth,
                filter_accessible_kb_ids,
            )
            from backend.db.connection import get_async_session_factory

            session_factory = get_async_session_factory()
            async with session_factory() as session:
                return await filter_accessible_kb_ids(
                    session,
                    kb_ids,
                    ApiKeyAuth(api_key="", user_id=user_id, role=role),
                )
        except Exception as exc:
            # Agent info is an information-disclosure boundary.  If the access
            # lookup fails, do not fall back to exposing binding IDs or labels.
            logger.warning(
                "[AgentInfoService] 知识库访问过滤失败，按不可见处理: %s",
                exc,
            )
            return []

    async def _fetch_kb_options(self, kb_ids: list[str]) -> list[dict[str, str]]:
        if not kb_ids:
            return []
        try:
            from sqlalchemy import select

            from backend.db.connection import get_async_session_factory
            from backend.db.models.knowledge_base import (
                KnowledgeBase,
                KnowledgeBaseStatus,
            )

            session_factory = get_async_session_factory()
            async with session_factory() as session:
                stmt = select(KnowledgeBase.kb_id, KnowledgeBase.kb_name).where(
                    KnowledgeBase.kb_id.in_(kb_ids),
                    KnowledgeBase.status == KnowledgeBaseStatus.ACTIVE,
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
        profile: AgentProfile,
        kb_ids: list[str],
        available_models: list[dict[str, Any]],
        use_search: bool,
        supports_delegation: bool,
    ) -> list[str]:
        features = [
            "guardrail",
            "context_summarization",
        ]
        if any(bool(item.get("supports_vision")) for item in available_models):
            features.append("vision")
        if self._supports_tool(profile, "generate_html_ppt"):
            features.append("file_presentation")
        if self._supports_tool(profile, "save_deep_research_report"):
            features.append("deep_research_report")
        if kb_ids:
            features.append("multi_kb_search")
        if use_search:
            features.append("web_search")
        if supports_delegation:
            features.append("task_delegation")
        return features
