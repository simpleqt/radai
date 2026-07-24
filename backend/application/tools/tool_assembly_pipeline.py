"""Tool assembly pipeline for platform agent executors."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool, tool

from backend.application.retrieval.query_resolution import effective_tool_query
from backend.application.tools.assembler import (
    PolicyToolAssembler,
    ToolAssemblyContext,
    tool_spec_metadata,
)
from backend.config.loggers import get_logger
from backend.core.skills.scoped_skills_provider import ScopedSkillsProvider
from backend.core.skills.skill_runtime_state import has_loaded_skill
from backend.domain.agents.retrieval_policy import RetrievalRuntimePolicy
from backend.domain.tools.runtime_context import ToolRuntimeContext
from backend.domain.tools.spec import ToolSpec

logger = get_logger(__name__)

CONTEXT_MANAGED_TOOLS = {
    "multi_kb_search",
    "chat_file_search",
    "save_deep_research_report",
    "web_search",
}

CONTROL_PLANE_TOOLS = {
    "read_local_skill_file",
    "read_skill_reference",
    "list_available_skills",
}

DISABLED_TOOLS = {
    "ask_clarification",
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
        enforce_skill_load: bool = True,
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
        self._enforce_skill_load = enforce_skill_load

    def _get_selected_skill_defs(self):
        if not self._selected_skills:
            return []
        skills = self._skills_provider.get_available_skills_for_user(
            selected_skills=self._selected_skills,
        )
        validator = getattr(self._skills_provider, "validate_skill_selection", None)
        if callable(validator):
            validator(skills)
        return skills

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
            logger.warning(
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
        workflow_name = self._effective_workflow_name()
        retrieval_policy = RetrievalRuntimePolicy.for_mode(
            lightweight=self._lightweight_mode,
            deep_research=workflow_name == "deep_research",
        )

        @tool("multi_kb_search", response_format="content_and_artifact")
        async def selected_multi_kb_search(
            query: str,
            high_level_keywords: str | list[str] | None = None,
            low_level_keywords: str | list[str] | None = None,
            query_type: str | None = None,
            top_k: int = retrieval_policy.kb_top_k,
            chunk_top_k: int = retrieval_policy.kb_chunk_top_k,
            merge_strategy: str = "rrf",
            max_result_chars: int | None = None,
        ) -> tuple[str, dict[str, Any]]:
            """搜索知识库；query 应上下文完整并保留本轮图表、公式、数值等限制。"""
            model_query = query
            # 普通 Full/轻量问答统一由检索底座抽取关键词与查询类型。
            # 深度研究的分支检索是显式规划结果，仍允许传入定向提示。
            allow_planned_hints = workflow_name == "deep_research"
            query = (
                model_query
                if allow_planned_hints
                else effective_tool_query(model_query)
            )
            effective_hl_keywords = high_level_keywords if allow_planned_hints else None
            effective_ll_keywords = low_level_keywords if allow_planned_hints else None
            effective_query_type = query_type if allow_planned_hints else None
            if query != model_query or (
                not allow_planned_hints
                and any((high_level_keywords, low_level_keywords, query_type))
            ):
                logger.info(
                    "[ToolAssembly] 检索查询保护: workflow=%s model_query=%s "
                    "effective_query=%s model_hints_ignored=%s",
                    workflow_name,
                    model_query[:160],
                    query[:240],
                    not allow_planned_hints
                    and bool(high_level_keywords or low_level_keywords or query_type),
                )
            if self._enforce_skill_load and self._selected_skills is not None:
                required_skill = str(self._domain or "").strip()
                if (
                    required_skill
                    and required_skill in self._selected_skills
                    and not has_loaded_skill(required_skill)
                ):
                    logger.warning(
                        "[ToolAssembly] multi_kb_search 被拦截：skill 未加载，skill=%s query=%s",
                        required_skill,
                        query,
                    )
                    message = (
                        "You MUST call read_local_skill_file"
                        f"(\"{required_skill}\") before multi_kb_search."
                    )
                    text = json.dumps(
                        {
                            "error": "skill_not_loaded",
                            "required_skill": required_skill,
                            "message": message,
                        },
                        ensure_ascii=False,
                    )
                    return text, {
                        "text": text,
                        "evidence_records": [],
                        "stats": {},
                    }
            effective_max_result_chars = max_result_chars
            if effective_max_result_chars is None and not self._lightweight_mode:
                effective_max_result_chars = MAX_RESULT_CHARS_FULL_AGENT

            # One public tool invocation maps to exactly one provider
            # retrieval. Query refinement belongs to the outer answer agent,
            # whose request-scoped budget allows a second distinct call. Do
            # not hide another plan/evaluate/search loop inside this tool.
            structured = await execute_multi_kb_search(
                query=query,
                kb_ids=list(self._kb_ids),
                top_k=(
                    min(top_k, retrieval_policy.kb_top_k)
                    if self._lightweight_mode
                    else top_k
                ),
                chunk_top_k=(
                    min(chunk_top_k, retrieval_policy.kb_chunk_top_k)
                    if self._lightweight_mode
                    else chunk_top_k
                ),
                merge_strategy=merge_strategy,
                external_hl_keywords=effective_hl_keywords,
                external_ll_keywords=effective_ll_keywords,
                external_query_type=effective_query_type,
                max_result_chars=effective_max_result_chars,
                _structured=True,
            )
            if not isinstance(structured, dict):
                structured = {
                    "text": str(structured or ""),
                    "evidence_records": [],
                    "stats": {},
                }
            return str(structured.get("text") or ""), structured

        return selected_multi_kb_search

    def _create_save_deep_research_report_tool(self):
        from backend.application.tools.providers.deep_research_report_tool import (
            save_deep_research_report_direct,
        )

        @tool("save_deep_research_report")
        async def selected_save_deep_research_report(
            title: str,
            content: str,
            filename: str | None = None,
        ) -> str:
            """保存深度研究 Markdown 报告，生成可下载 DOCX 工件。"""
            if not self._thread_id:
                return json.dumps(
                    {"error": "thread_id is not available for report save"},
                    ensure_ascii=False,
                )
            payload = await save_deep_research_report_direct(
                thread_id=self._thread_id,
                title=title,
                content=content,
                filename=filename,
            )
            return json.dumps(payload, ensure_ascii=False)

        return selected_save_deep_research_report

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
        allowed = {spec.name for spec in self._resolve_policy_tool_specs()}
        # A Skill declaration is a capability request, not an unrestricted
        # escape hatch.  Permit it only when the registry contract supports the
        # current domain/workflow.  Write tools must still be explicitly mapped
        # by Policy so an uploaded Skill cannot silently gain artifact writes.
        context = self._tool_assembly_context()
        if context is None:
            return allowed
        for declared_name in self._declared_skill_tool_names():
            if declared_name in allowed:
                continue
            try:
                spec = self._policy_tool_assembler.get_spec(declared_name)
            except Exception:
                logger.warning(
                    "[ToolAssembly] 声明工具未在注册表中找到: tool=%s",
                    declared_name,
                    exc_info=True,
                )
                continue
            if not spec.supports(
                domain=context.domain,
                workflow=context.workflow,
                selected_skills=context.selected_skills,
            ):
                continue
            # A custom Skill may only expand the registry through an explicit
            # Skill-bound spec.  Generic safe tools still need to be mapped in
            # the policy; otherwise an uploaded SKILL.md could enumerate the
            # entire registry as an implicit capability grant.
            if not spec.required_skills or spec.required_skills.isdisjoint(
                context.selected_skills
            ):
                continue
            if spec.risk_level == "write":
                continue
            allowed.add(declared_name)
        return allowed

    def _declared_skill_tool_names(self) -> set[str]:
        return {
            str(tool_name).strip()
            for skill in self._get_selected_skill_defs()
            for tool_name in getattr(skill, "tool_names", [])
            if str(tool_name).strip()
        }

    def _acceptable_missing_tools(self) -> set[str]:
        missing: set[str] = set()
        if not self._kb_ids:
            missing.add("multi_kb_search")
        if not self._use_search:
            missing.add("web_search")
        if not self._has_chat_file_search_index():
            missing.add("chat_file_search")
        # A provider dependency or upstream client may be unavailable on one
        # worker.  The assembler records that state and removes only the
        # affected capability; Skill validation must not turn graceful
        # degradation back into a request-wide failure.
        reporter = getattr(self._policy_tool_assembler, "health_report", None)
        if callable(reporter):
            missing.update(
                item.name
                for item in reporter()
                if getattr(item, "status", "") == "unavailable"
            )
        return missing

    def _has_chat_file_search_index(self) -> bool:
        user_id = str(getattr(self._tool_runtime_context, "user_id", "") or "").strip()
        if not self._thread_id or not user_id:
            return False
        try:
            from backend.application.tools.providers.chat_file_search_tool import (
                has_thread_file_index,
            )

            return bool(has_thread_file_index(self._thread_id, user_id=user_id))
        except Exception as exc:
            logger.warning("[ToolAssembly] chat_file_search 索引检查失败: %s", exc)
            return False

    def _is_declared_tool_out_of_scope(self, tool_name: str) -> bool:
        context = self._tool_assembly_context()
        if context is None:
            return False
        try:
            spec = self._policy_tool_assembler.get_spec(tool_name)
        except Exception:
            logger.warning(
                "[ToolAssembly] 无法检查声明工具范围: tool=%s",
                tool_name,
                exc_info=True,
            )
            return False
        return not spec.supports(
            domain=context.domain,
            workflow=context.workflow,
            selected_skills=context.selected_skills,
        )

    def _inject_chat_file_search_tool(self, tools_by_name: dict[str, BaseTool], allowed_tool_names: set[str]) -> None:
        user_id = str(getattr(self._tool_runtime_context, "user_id", "") or "").strip()
        if not self._thread_id or not user_id or "chat_file_search" not in allowed_tool_names:
            return
        try:
            from backend.application.tools.providers.chat_file_search_tool import (
                create_scoped_chat_file_search_tool,
            )

            if self._has_chat_file_search_index():
                file_search = create_scoped_chat_file_search_tool(
                    self._thread_id,
                    user_id=user_id,
                )
                tools_by_name[file_search.name] = file_search
                logger.info(
                    "[ToolAssembly] 已注入 chat_file_search 工具（thread_id=%s）",
                    self._thread_id,
                )
        except Exception as e:
            logger.warning("[ToolAssembly] chat_file_search 注入失败: %s", e)

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
        unacceptable_missing = audit.missing - self._acceptable_missing_tools()
        if unacceptable_missing:
            raise RuntimeError(
                "[ToolAssembly] ToolPolicy 期望工具缺失: policy=%s domain=%s workflow=%s missing=%s"
                % (
                    audit.policy_name,
                    audit.domain,
                    audit.workflow,
                    sorted(unacceptable_missing),
                )
            )
        declared_allowed = self._declared_skill_tool_names() & set(tools_by_name)
        unexpected = audit.unexpected - declared_allowed
        if unexpected:
            raise RuntimeError(
                "[ToolAssembly] ToolPolicy 发现未授权业务工具: policy=%s domain=%s workflow=%s unexpected=%s"
                % (
                    audit.policy_name,
                    audit.domain,
                    audit.workflow,
                    sorted(unexpected),
                )
            )
        logger.info(
            "[ToolAssembly] 策略执行: policy=%s domain=%s workflow=%s expected=%s missing=%s unexpected=%s",
            audit.policy_name,
            audit.domain,
            audit.workflow,
            sorted(audit.expected),
            sorted(audit.missing),
            sorted(unexpected),
        )

    def _apply_policy_metadata(self, tools_by_name: dict[str, BaseTool]) -> None:
        for tool_name, tool_item in list(tools_by_name.items()):
            try:
                spec = self._policy_tool_assembler.get_spec(tool_name)
            except Exception:
                logger.warning(
                    "[ToolAssembly] 无法读取工具策略元数据: tool=%s",
                    tool_name,
                    exc_info=True,
                )
                continue
            metadata = dict(getattr(tool_item, "metadata", None) or {})
            for key, value in tool_spec_metadata(spec).items():
                metadata.setdefault(key, value)
            try:
                tool_item.metadata = metadata
            except Exception:
                logger.warning(
                    "[ToolAssembly] 工具元数据写入失败，已跳过: tool=%s",
                    tool_name,
                    exc_info=True,
                )

    def health_report(self):
        """Return health observed while materializing this pipeline's tools."""

        reporter = getattr(self._policy_tool_assembler, "health_report", None)
        return reporter() if callable(reporter) else []

    def assemble(self) -> list[BaseTool]:
        from backend.application.tools.providers.async_web_search_tool import (
            get_async_web_search_tool,
        )
        from backend.core.skills.native_skill_tools import create_native_skill_tools

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
            save_report_tool = self._create_save_deep_research_report_tool()
            tools_by_name[save_report_tool.name] = save_report_tool

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
        if selected_skill_defs:
            self._inject_declared_skill_tools(
                tools_by_name=tools_by_name,
                skills=selected_skill_defs,
                allowed_tool_names=allowed_tool_names,
            )
        self._inject_chat_file_search_tool(tools_by_name, allowed_tool_names)

        if self._selected_skills:
            self._validate_skill_tool_declarations(
                tools_by_name,
                skills=selected_skill_defs,
            )

        self._apply_policy_metadata(tools_by_name)
        self._audit_policy_alignment(tools_by_name)
        logger.info("[ToolAssembly] 组装完成: %s", sorted(tools_by_name.keys()))
        return list(tools_by_name.values())

    def _inject_declared_skill_tools(
        self,
        *,
        tools_by_name: dict[str, BaseTool],
        skills: list[Any],
        allowed_tool_names: set[str] | None = None,
    ) -> None:
        allowed_tool_names = allowed_tool_names or self._policy_allowed_tool_names()
        for skill in skills:
            for declared_tool_name in skill.tool_names:
                if declared_tool_name not in allowed_tool_names:
                    logger.warning(
                        "[ToolAssembly] Skill 声明工具未通过当前 Policy/范围校验: skill=%s tool=%s",
                        skill.name,
                        declared_tool_name,
                    )
                    continue
                result = self._load_declared_skill_tool(declared_tool_name)
                if result is None:
                    continue
                # 支持单个工具或工具列表（如 excel-experiment-analysis 返回多个工具）
                tool_list = result if isinstance(result, list) else [result]
                for skill_tool in tool_list:
                    if skill_tool.name not in allowed_tool_names:
                        continue
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
                if missing == ["chat_file_search"] or "chat_file_search" in missing:
                    raise RuntimeError(
                        "[ToolAssembly] Skill '%s' 声明 chat_file_search，但当前线程没有可用文件索引"
                        % skill.name
                    )
                unacceptable = [
                    tool_name
                    for tool_name in missing
                    if tool_name not in self._acceptable_missing_tools()
                    and not self._is_declared_tool_out_of_scope(tool_name)
                ]
                if not unacceptable:
                    logger.info(
                        "[ToolAssembly] Skill '%s' 声明的工具当前条件或 workflow 不可用，已允许缺失: %s",
                        skill.name,
                        missing,
                    )
                    continue
                raise RuntimeError(
                    "[ToolAssembly] Skill '%s' 声明的工具在当前工具集中缺失: %s"
                    % (skill.name, unacceptable)
                )
            logger.info(
                "[ToolAssembly] Skill '%s' 工具声明验证通过: %s",
                skill.name,
                skill.tool_names,
            )
