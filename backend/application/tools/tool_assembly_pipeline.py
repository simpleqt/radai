"""Tool assembly pipeline for platform agent executors."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool, tool

from backend.application.tools.assembler import PolicyToolAssembler, ToolAssemblyContext
from backend.config.loggers import get_logger
from backend.core.skills.scoped_skills_provider import ScopedSkillsProvider
from backend.core.skills.skill_runtime_state import has_loaded_skill
from backend.domain.tools.spec import ToolSpec
from backend.domain.tools.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)

CONTEXT_MANAGED_TOOLS = {
    "multi_kb_search",
    "chat_file_search",
    "save_deep_research_report",
}

CONTROL_PLANE_TOOLS = {
    "read_local_skill_file",
    "read_skill_reference",
    "list_available_skills",
}

DISABLED_TOOLS = {
    "ask_clarification",
}

KB_SKILL_ENFORCEMENT: dict[str, str] = {
    "nuclear-electronics": 'You MUST call read_local_skill_file("nuclear-electronics") before multi_kb_search.',
    "csh-hydrogeology": 'You MUST call read_local_skill_file("csh-hydrogeology") before multi_kb_search.',
}


class ToolAssemblyPipeline:
    """Assemble request-scoped tool sets for the main agent."""

    def __init__(
        self,
        *,
        model_name: str | None = None,
        subagent_enabled: bool = False,
        use_search: bool = True,
        kb_ids: list[str] | None = None,
        selected_skills: set[str] | None = None,
        skills_provider: ScopedSkillsProvider | None = None,
        task_type: str | None = None,
        lightweight_mode: bool = False,
        thread_id: str | None = None,
        domain: str | None = None,
        workflow_name: str | None = None,
        tool_policy: str | None = None,
        policy_tool_assembler: PolicyToolAssembler | None = None,
        tool_runtime_context: ToolRuntimeContext | None = None,
    ):
        self._model_name = model_name
        self._subagent_enabled = subagent_enabled
        self._use_search = use_search
        self._kb_ids = kb_ids if kb_ids is not None else []
        self._selected_skills = selected_skills
        self._skills_provider = skills_provider or ScopedSkillsProvider()
        self._task_type = task_type
        self._lightweight_mode = lightweight_mode
        self._thread_id = thread_id
        self._domain = domain
        self._workflow_name = workflow_name
        self._tool_policy = tool_policy
        self._policy_tool_assembler = policy_tool_assembler or PolicyToolAssembler()
        self._tool_runtime_context = tool_runtime_context

    def _get_selected_skill_defs(self):
        if not self._selected_skills:
            return []
        return self._skills_provider.get_available_skills_for_user(
            selected_skills=self._selected_skills,
        )

    def _load_declared_skill_tool(self, tool_name: str):
        if tool_name in CONTEXT_MANAGED_TOOLS:
            return None
        try:
            tools = self._policy_tool_assembler.materialize_tool_name(
                tool_name,
                runtime_context=self._tool_runtime_context,
            )
            if not tools:
                return None
            return tools if len(tools) > 1 else tools[0]
        except Exception as exc:
            logger.debug(
                "[ToolAssembly] 按 ToolRegistry 加载工具失败，跳过声明工具: tool=%s error=%s",
                tool_name,
                exc,
            )
        return None

    def _create_multi_kb_search_tool(self):
        from backend.application.tools.providers.multi_kb_search_tool import (
            MAX_RESULT_CHARS_FULL_AGENT,
            execute_multi_kb_search,
        )

        max_result_chars = (
            MAX_RESULT_CHARS_FULL_AGENT if not self._lightweight_mode else None
        )

        @tool("multi_kb_search")
        async def selected_multi_kb_search(
            query: str,
            high_level_keywords: str | list[str] | None = None,
            low_level_keywords: str | list[str] | None = None,
            query_type: str | None = None,
            top_k: int = 10,
            chunk_top_k: int = 6,
            merge_strategy: str = "rrf",
        ) -> str:
            """搜索当前选中的知识库获取相关信息。单次按完整 chunk 返回约 5000 字，可多次调用补充证据。"""
            if self._selected_skills is not None:
                for required_skill, message in KB_SKILL_ENFORCEMENT.items():
                    if required_skill in self._selected_skills and not has_loaded_skill(required_skill):
                        logger.warning(
                            "[ToolAssembly] multi_kb_search 被拦截：skill 未加载，skill=%s query=%s",
                            required_skill,
                            query,
                        )
                        return json.dumps(
                            {
                                "error": "skill_not_loaded",
                                "required_skill": required_skill,
                                "message": message,
                            },
                            ensure_ascii=False,
                        )
            return await execute_multi_kb_search(
                query=query,
                kb_ids=list(self._kb_ids),
                top_k=min(top_k, 12) if self._lightweight_mode else top_k,
                chunk_top_k=min(chunk_top_k, 6) if self._lightweight_mode else chunk_top_k,
                merge_strategy=merge_strategy,
                external_hl_keywords=high_level_keywords,
                external_ll_keywords=low_level_keywords,
                external_query_type=query_type,
                max_result_chars=max_result_chars,
            )

        return selected_multi_kb_search

    def _effective_workflow_name(self) -> str:
        if self._workflow_name:
            return self._workflow_name
        if self._lightweight_mode:
            return "lightweight"
        if self._task_type == "deep_research":
            return "deep_research"
        return "full"

    def _tool_assembly_context(self) -> ToolAssemblyContext | None:
        if not (self._domain and self._tool_policy):
            return None
        return ToolAssemblyContext(
            domain=self._domain,
            workflow=self._effective_workflow_name(),
            policy_name=self._tool_policy,
            selected_skills=self._selected_skills or set(),
        )

    def _resolve_policy_tool_specs(self) -> list[ToolSpec]:
        context = self._tool_assembly_context()
        if context is None:
            return []
        return self._policy_tool_assembler.resolve_specs(context)

    def _policy_allowed_tool_names(self) -> set[str]:
        return {spec.name for spec in self._resolve_policy_tool_specs()}

    def _audit_policy_alignment(self, tools_by_name: dict[str, BaseTool]) -> None:
        context = self._tool_assembly_context()
        if context is None:
            return
        business_tool_names = set(tools_by_name) - CONTROL_PLANE_TOOLS
        audit = self._policy_tool_assembler.audit(
            context,
            actual_tool_names=business_tool_names,
        )
        if not audit.expected:
            return
        logger.info(
            "[ToolAssembly] 策略执行: policy=%s domain=%s workflow=%s expected=%s missing=%s unexpected=%s",
            audit.policy_name,
            audit.domain,
            audit.workflow,
            sorted(audit.expected),
            sorted(audit.missing),
            sorted(audit.unexpected),
        )

    def assemble(self) -> list[BaseTool]:
        if self._lightweight_mode:
            logger.info("[ToolAssembly] 轻量问答模式：跳过工具注入")
            return []

        from backend.core.skills.native_skill_tools import create_native_skill_tools
        from backend.application.tools.providers.async_web_search_tool import get_async_web_search_tool
        from backend.application.tools.providers.deep_research_report_tool import save_deep_research_report_tool

        allowed_tool_names = self._policy_allowed_tool_names()
        if not allowed_tool_names:
            logger.warning(
                "[ToolAssembly] ToolPolicy 未解析出可用工具，已拒绝旧工具兜底: policy=%s domain=%s workflow=%s",
                self._tool_policy,
                self._domain,
                self._effective_workflow_name(),
            )

        if self._tool_assembly_context() is None:
            tools_by_name = {}
        elif self._tool_runtime_context is None:
            tools_by_name = self._policy_tool_assembler.materialize_policy_tools(
                self._tool_assembly_context(),
                skip_tool_names=CONTEXT_MANAGED_TOOLS,
            )
        else:
            tools_by_name = self._policy_tool_assembler.materialize_policy_tools(
                self._tool_assembly_context(),
                skip_tool_names=CONTEXT_MANAGED_TOOLS,
                runtime_context=self._tool_runtime_context,
            )
        for disabled_tool_name in DISABLED_TOOLS:
            tools_by_name.pop(disabled_tool_name, None)
        logger.info(
            "[ToolAssembly] 已按 ToolPolicy 注入静态工具=%s",
            sorted(tools_by_name),
        )

        if "multi_kb_search" in allowed_tool_names and self._kb_ids:
            selected_multi_kb_search = self._create_multi_kb_search_tool()
            tools_by_name[selected_multi_kb_search.name] = selected_multi_kb_search
            logger.info("[ToolAssembly] 已注入 multi_kb_search（kb_ids=%s）", self._kb_ids)
        elif "multi_kb_search" in allowed_tool_names:
            logger.info("[ToolAssembly] 已禁用 multi_kb_search（kb_ids 为空）")

        if "save_deep_research_report" in allowed_tool_names:
            tools_by_name[save_deep_research_report_tool.name] = save_deep_research_report_tool

        if self._use_search and "web_search" in allowed_tool_names:
            tools_by_name["web_search"] = get_async_web_search_tool()
            logger.info("[ToolAssembly] 已注入 web_search（use_search=True）")
        elif "web_search" in allowed_tool_names:
            logger.info("[ToolAssembly] 已禁用 web_search（use_search=False）")

        native_skill_tools = create_native_skill_tools(
            self._skills_provider,
            selected_skills=self._selected_skills,
        )
        if self._selected_skills:
            for skill_tool in native_skill_tools:
                tools_by_name[skill_tool.name] = skill_tool
            logger.info(
                "[ToolAssembly] 已注入原生 Skill 控制面工具集=%s",
                [tool_item.name for tool_item in native_skill_tools],
            )

        selected_skill_defs = self._get_selected_skill_defs()

        if self._selected_skills:
            self._validate_skill_tool_declarations(
                tools_by_name,
                skills=selected_skill_defs,
            )

        # 注入对话文件检索工具（如果当前会话有长文档索引）
        if self._thread_id and "chat_file_search" in allowed_tool_names:
            try:
                from backend.application.tools.providers.chat_file_search_tool import (
                    create_scoped_chat_file_search_tool,
                    has_thread_file_index,
                )
                if has_thread_file_index(self._thread_id):
                    file_search = create_scoped_chat_file_search_tool(self._thread_id)
                    tools_by_name[file_search.name] = file_search
                    logger.info(
                        "[ToolAssembly] 已注入 chat_file_search 工具（thread_id=%s）",
                        self._thread_id,
                    )
            except Exception as e:
                logger.warning("[ToolAssembly] chat_file_search 注入失败: %s", e)

        self._audit_policy_alignment(tools_by_name)
        logger.info("[ToolAssembly] 组装完成: %s", sorted(tools_by_name.keys()))
        return list(tools_by_name.values())

    def _inject_declared_skill_tools(
        self,
        *,
        tools_by_name: dict[str, BaseTool],
        skills: list[Any],
    ) -> None:
        for skill in skills:
            for declared_tool_name in skill.tool_names:
                result = self._load_declared_skill_tool(declared_tool_name)
                if result is None:
                    continue
                # 支持单个工具或工具列表（如 excel-experiment-analysis 返回多个工具）
                tool_list = result if isinstance(result, list) else [result]
                for skill_tool in tool_list:
                    tools_by_name[skill_tool.name] = skill_tool
                    logger.info(
                        "[ToolAssembly] 已按 Skill 声明注入工具: skill=%s tool=%s",
                        skill.name,
                        skill_tool.name,
                    )

    def _validate_skill_tool_declarations(
        self,
        tools_by_name: dict[str, Any],
        *,
        skills: list[Any] | None = None,
    ) -> None:
        skills = skills if skills is not None else self._get_selected_skill_defs()
        for skill in skills:
            if not skill.tool_names:
                continue
            missing = [tool_name for tool_name in skill.tool_names if tool_name not in tools_by_name]
            if missing:
                logger.warning(
                    "[ToolAssembly] Skill '%s' 声明的工具在当前工具集中缺失: %s",
                    skill.name,
                    missing,
                )
                continue
            logger.info(
                "[ToolAssembly] Skill '%s' 工具声明验证通过: %s",
                skill.name,
                skill.tool_names,
            )
