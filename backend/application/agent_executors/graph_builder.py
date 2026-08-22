"""Platform LangGraph builder for profiled full-feature agents."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from backend.application.agents.prompt_builder import PromptBuildContext, PromptBuilder
from backend.core.skills.scoped_skills_provider import ScopedSkillsProvider
from backend.domain.agents.runtime_agent import RuntimeAgentSpec

logger = logging.getLogger(__name__)


def _tool_metadata_by_name(tools: list[Any]) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for tool in tools:
        name = str(getattr(tool, "name", "") or "")
        raw_metadata = getattr(tool, "metadata", None)
        if name and isinstance(raw_metadata, dict):
            metadata[name] = dict(raw_metadata)
    return metadata


def _resolve_domain_skill_name(
    domain: str,
    selected_skills: set[str],
) -> str | None:
    """Resolve the conventional domain skill without a central domain map."""

    normalized_domain = str(domain or "").strip()
    return normalized_domain if normalized_domain in selected_skills else None


@dataclass(frozen=True)
class ProfiledGraphBuildConfig:
    label: str
    graph_name: str
    domain: str
    tool_policy: str
    workflow_name: str | None
    model_name: str | None
    thinking_enabled: bool
    subagent_enabled: bool
    use_search: bool
    kb_ids: list[str]
    selected_skills: set[str] | None
    skills_provider: ScopedSkillsProvider
    task_type: str | None
    lightweight_mode: bool
    lightweight_has_kb: bool
    thread_id: str | None
    external_thread_id: str | None
    max_concurrent_subagents: int
    checkpointer: BaseCheckpointSaver | None
    guardrail_policy: str = "default"
    deep_retrieval: bool = False
    prompt_profile: str | None = None
    deep_research_task_type: str = "deep_research"
    tool_runtime_context: Any | None = None
    runtime_permissions: dict[str, Any] | None = None
    guardrail_instructions: str = ""
    time_context: str = ""
    request_metadata: dict[str, Any] | None = None
    context_policy: Any | None = None
    runtime_agent_spec: RuntimeAgentSpec | None = None
    prompt_provider: Any | None = None
    policy_tool_assembler: Any | None = None


@dataclass(frozen=True)
class ProfiledGraphBuildResult:
    graph: CompiledStateGraph
    model_name: str | None
    tools: list[Any]
    compaction_checkpoints: Any | None = None
    context_pruning_middleware: Any | None = None


class ProfiledGraphBuilder:
    """Build LangGraph graphs from normalized agent profile/runtime config."""

    def __init__(self, config: ProfiledGraphBuildConfig):
        self._config = config

    def build(self) -> ProfiledGraphBuildResult:
        from backend.core.agents import RuntimeFeatures, ThreadState, create_agent_graph
        from backend.core.agents.config_compat import get_default_model_name
        from backend.core.agents.context import ContextPolicy
        from backend.core.agents.middlewares.context_pruning import (
            ContextPruningMiddleware,
        )
        from backend.core.agents.middlewares.groundwater_project_workflow import (
            GroundwaterProjectWorkflowMiddleware,
        )
        from backend.core.agents.middlewares.model_turn_limit import (
            ModelTurnLimitMiddleware,
        )
        from backend.core.agents.middlewares.runtime_policy import (
            RuntimePolicyGuardrailProvider,
        )
        from backend.core.agents.middlewares.skill_load_guard import (
            SkillLoadGuardMiddleware,
        )
        from backend.core.agents.middlewares.subagent_limit import (
            SubagentLimitMiddleware,
        )
        from backend.core.agents.middlewares.tool_guardrail import (
            ToolGuardrailMiddleware,
        )
        from backend.core.models import create_chat_model

        cfg = self._config
        build_started = time.perf_counter()
        model = create_chat_model(
            name=cfg.model_name,
            thinking_enabled=cfg.thinking_enabled,
        )
        model_seconds = time.perf_counter() - build_started
        effective_model_name = cfg.model_name
        if effective_model_name is None:
            default_name = get_default_model_name()
            if default_name:
                effective_model_name = default_name
                logger.info("[%s] 使用默认模型: %s", cfg.label, effective_model_name)
        context_policy = cfg.context_policy or ContextPolicy.from_env(
            effective_model_name
        )

        logger.info("[%s] 创建模型: %s", cfg.label, type(model).__name__)

        tools_started = time.perf_counter()
        tools = self._assemble_tools(model_name=effective_model_name)
        tools_seconds = time.perf_counter() - tools_started
        logger.info("[%s] 工具列表: %s", cfg.label, [tool.name for tool in tools])

        feature_tool_names = {"task"} if cfg.subagent_enabled else set()

        guardrail_mw = ToolGuardrailMiddleware(
            provider=RuntimePolicyGuardrailProvider(
                allowed_tools=[
                    *[tool.name for tool in tools],
                    *sorted(feature_tool_names),
                ],
                guardrail_policy=cfg.guardrail_policy,
                permissions=dict(cfg.runtime_permissions or {}),
            ),
            passport=cfg.guardrail_policy,
            tool_metadata=_tool_metadata_by_name(tools),
        )

        subagent_feature: bool | Any = False
        if cfg.subagent_enabled:
            subagent_feature = SubagentLimitMiddleware(
                max_concurrent=cfg.max_concurrent_subagents
            )

        features = RuntimeFeatures(
            # ContextPruningMiddleware is the single history/JIT compaction
            # owner. Keeping the framework summarizer in the chain would
            # rewrite the same window a second time and invalidate cache keys.
            summarization=False,
            subagent=subagent_feature,
            clarification=False,
            guardrail=guardrail_mw,
        )

        context_pruning = ContextPruningMiddleware(
            thread_id=cfg.thread_id,
            model_name=effective_model_name,
            user_id=str(getattr(cfg.tool_runtime_context, "user_id", "") or "") or None,
            # Install the capability ledger under the execution-scope key so
            # parallel coordination units of one request never overwrite each
            # other's registry entry; scoped tools look it up by the same key.
            request_id=(
                str(
                    getattr(cfg.tool_runtime_context, "context_scope_id", "")
                    or getattr(cfg.tool_runtime_context, "request_id", "")
                    or ""
                )
                or None
            ),
            policy=context_policy,
            checkpointer=cfg.checkpointer,
        )
        extra_middleware: list[Any] = [context_pruning]
        if cfg.runtime_agent_spec is not None:
            extra_middleware.append(
                ModelTurnLimitMiddleware(cfg.runtime_agent_spec.max_turns)
            )
        if cfg.selected_skills:
            control_plane_tools = {
                "read_local_skill_file",
                "read_skill_reference",
                "list_available_skills",
            }
            selected_skills = set(cfg.selected_skills)
            guard_groups: dict[tuple[str, tuple[str, ...]], set[str]] = {}
            # A domain skill is declared by selecting a skill with the same
            # package key as the profile domain. This keeps KB skill loading
            # extensible without adding a Python branch for every new agent.
            domain_kb_skill = _resolve_domain_skill_name(
                cfg.domain,
                selected_skills,
            )
            for tool_item in tools:
                if tool_item.name in control_plane_tools:
                    continue
                metadata = (
                    dict(tool_item.metadata)
                    if isinstance(getattr(tool_item, "metadata", None), dict)
                    else {}
                )
                required_skills = {
                    str(item).strip()
                    for item in metadata.get("required_skills", [])
                    if str(item or "").strip()
                }
                if tool_item.name == "multi_kb_search" and domain_kb_skill:
                    required_skills.add(domain_kb_skill)
                relevant_skills = required_skills & selected_skills
                if not relevant_skills:
                    continue
                mode = str(metadata.get("required_skill_mode") or "any").strip().lower()
                key = (
                    mode if mode in {"any", "all"} else "any",
                    tuple(sorted(relevant_skills)),
                )
                guard_groups.setdefault(key, set()).add(tool_item.name)

            for index, ((mode, required_skills), guarded_tools) in enumerate(
                sorted(guard_groups.items())
            ):
                extra_middleware.insert(
                    index,
                    SkillLoadGuardMiddleware(
                        required_skills=set(required_skills),
                        required_skill_mode=mode,
                        guarded_tools=guarded_tools,
                    ),
                )
            if (
                cfg.domain == "groundwater-project-analysis"
                and "groundwater-project-analysis" in cfg.selected_skills
            ):
                extra_middleware.append(GroundwaterProjectWorkflowMiddleware())

        self._materialized_tool_names = {
            *[tool.name for tool in tools],
            *feature_tool_names,
        }
        prompt_started = time.perf_counter()
        system_prompt = self._build_system_prompt()
        prompt_seconds = time.perf_counter() - prompt_started
        graph_started = time.perf_counter()
        graph = create_agent_graph(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            features=features,
            extra_middleware=extra_middleware,
            checkpointer=cfg.checkpointer,
            name=cfg.graph_name,
            state_schema=ThreadState,
        )
        graph_seconds = time.perf_counter() - graph_started
        # 每请求全量重建执行器是已知成本;分段耗时为「装配缓存/编译图缓存」
        # 的决策提供数据,total 含未单列的中间件装配等杂项。
        logger.info(
            "[%s] 图构建完成: %s (耗时ms model=%.1f tools=%.1f prompt=%.1f "
            "graph=%.1f total=%.1f)",
            cfg.label,
            graph.name,
            model_seconds * 1000,
            tools_seconds * 1000,
            prompt_seconds * 1000,
            graph_seconds * 1000,
            (time.perf_counter() - build_started) * 1000,
        )
        return ProfiledGraphBuildResult(
            graph=graph,
            model_name=effective_model_name,
            tools=tools,
            compaction_checkpoints=context_pruning.compaction_checkpoints,
            context_pruning_middleware=context_pruning,
        )

    def _build_system_prompt(
        self,
        *,
        available_tool_names: set[str] | None = None,
    ) -> str:
        cfg = self._config
        if available_tool_names is None:
            available_tool_names = getattr(
                self,
                "_materialized_tool_names",
                None,
            )
        if cfg.lightweight_mode:
            base_prompt = PromptBuilder.build_runtime_context(
                PromptBuildContext(
                    domain=cfg.domain,
                    selected_skills=cfg.selected_skills,
                    subagent_enabled=cfg.subagent_enabled,
                    use_search=cfg.use_search,
                    prompt_profile=cfg.prompt_profile,
                    task_type=cfg.task_type,
                    lightweight_mode=True,
                    lightweight_has_kb=cfg.lightweight_has_kb,
                    guardrail_instructions=cfg.guardrail_instructions,
                    time_context=cfg.time_context,
                    request_metadata=dict(cfg.request_metadata or {}),
                )
            )
        else:
            base_prompt = PromptBuilder(
                skills_provider=cfg.skills_provider,
                prompt_provider=cfg.prompt_provider,
            ).build(
                PromptBuildContext(
                    domain=cfg.domain,
                    selected_skills=cfg.selected_skills,
                    subagent_enabled=cfg.subagent_enabled,
                    use_search=cfg.use_search,
                    prompt_profile=cfg.prompt_profile,
                    task_type=cfg.task_type,
                    lightweight_mode=cfg.lightweight_mode,
                    lightweight_has_kb=cfg.lightweight_has_kb,
                    guardrail_instructions=cfg.guardrail_instructions,
                    time_context=cfg.time_context,
                    request_metadata=dict(cfg.request_metadata or {}),
                )
            )
        return self._append_runtime_agent_contract(
            base_prompt,
            available_tool_names=available_tool_names,
        )

    def _append_runtime_agent_contract(
        self,
        base_prompt: str,
        *,
        available_tool_names: set[str] | None = None,
    ) -> str:
        """Bind coordinated runtime roles at system-prompt priority."""

        spec = self._config.runtime_agent_spec
        if spec is None:
            return base_prompt
        if available_tool_names is None:
            effective_allowed_tools = spec.allowed_tools
        elif spec.allowed_tools is None:
            effective_allowed_tools = set(available_tool_names)
        else:
            effective_allowed_tools = set(spec.allowed_tools) & set(
                available_tool_names
            )
        allowed_tools = (
            "由当前领域工具策略决定"
            if effective_allowed_tools is None
            else ", ".join(sorted(effective_allowed_tools)) or "无"
        )
        required_tools = ", ".join(sorted(spec.required_tools)) or "无"
        unavailable_required_tools = (
            set()
            if available_tool_names is None
            else set(spec.required_tools) - set(available_tool_names)
        )
        unavailable_required = ", ".join(sorted(unavailable_required_tools)) or "无"
        disallowed_tools = ", ".join(sorted(spec.disallowed_tools)) or "无"
        output_schema = spec.output_schema or "未指定"
        output_schema_note = (
            "（这是编排层的输出契约标签：研究计划会被严格解析，SubagentResult 会在模型完成后由运行时封装，"
            "Markdown 输出会经过证据校验；不要自行伪造交接信封）"
            if spec.output_schema
            else ""
        )
        artifact_rule = (
            "context_read 只接受【证据台账】列出的 sid（句柄为兼容旧调用的可选参数）；"
            "read_context_artifact 只接受系统明确提供的 artifact://context/... 引用；"
            "网页 URL、知识库引用、source_id 和猜测路径都不是 artifact 引用，必须直接作为证据使用，"
            "不得传给该工具；context_read 失败时禁止重新发起知识库检索。\n"
            if effective_allowed_tools is None
            or "read_context_artifact" in effective_allowed_tools
            or "context_read" in effective_allowed_tools
            else ""
        )
        role_contract = (
            "<runtime_agent_contract>\n"
            f"角色名称：{spec.name}\n"
            "这是当前隔离执行单元的权威角色契约。若顶层领域提示中的流程、工具要求或输出要求"
            "与本契约冲突，以本契约为准；仍须遵守安全、权限和工具运行时限制。\n"
            f"角色职责：\n{spec.system_prompt}\n"
            f"当前实际可用工具：{allowed_tools}\n"
            f"{artifact_rule}"
            f"必须工具：{required_tools}\n"
            f"当前缺失的必须工具：{unavailable_required}\n"
            f"禁止工具：{disallowed_tools}\n"
            f"可联网：{'是' if spec.can_use_network else '否'}\n"
            f"可读取知识库：{'是' if spec.can_read_kb else '否'}\n"
            f"最大模型轮次：{spec.max_turns}\n"
            "轮次收敛规则：必须为最终总结预留最后一个模型轮次；证据已足够、工具失败，"
            "或已进入最后允许轮次时，立即停止调用工具并输出当前最佳结论与证据缺口。\n"
            f"期望输出结构：{output_schema}{output_schema_note}\n"
            "工具不可用时不得假装调用；结果汇总角色只基于输入交接内容作答。\n"
            "</runtime_agent_contract>"
        )
        return f"{base_prompt.rstrip()}\n\n{role_contract}"

    def _assemble_tools(self, *, model_name: str | None) -> list[Any]:
        from backend.application.tools.tool_assembly_pipeline import (
            ToolAssemblyPipeline,
        )

        cfg = self._config
        if cfg.request_metadata and cfg.request_metadata.get("tool_access") is False:
            # Coordination writers/reviewers receive an evidence handoff, not
            # an implicit second chance to query project data.  This must be a
            # capability boundary at assembly time, not merely a prompt hint.
            logger.info("[%s] result-only unit: tool assembly disabled", cfg.label)
            return []
        workflow_name = cfg.workflow_name or (
            "lightweight" if cfg.lightweight_mode else cfg.task_type or "full"
        )
        tools = ToolAssemblyPipeline(
            model_name=model_name,
            subagent_enabled=cfg.subagent_enabled,
            use_search=cfg.use_search,
            deep_retrieval=cfg.deep_retrieval,
            kb_ids=list(cfg.kb_ids),
            selected_skills=cfg.selected_skills,
            skills_provider=cfg.skills_provider,
            task_type=cfg.task_type,
            lightweight_mode=cfg.lightweight_mode,
            thread_id=cfg.thread_id,
            domain=cfg.domain,
            workflow_name=workflow_name,
            tool_policy=cfg.tool_policy,
            policy_tool_assembler=cfg.policy_tool_assembler,
            tool_runtime_context=cfg.tool_runtime_context,
            enforce_skill_load=not cfg.lightweight_mode,
        ).assemble()
        # Plan-Solve branches have an explicit capability contract.  Without
        # this boundary every branch receives the whole ``full`` tool set and
        # can repeat orchestration-layer retrieval or use an unrelated tool.
        allowed = None
        if (
            cfg.request_metadata
            and "coordination_allowed_tools" in cfg.request_metadata
        ):
            raw_allowed = cfg.request_metadata.get("coordination_allowed_tools") or []
            allowed = {str(name).strip() for name in raw_allowed if str(name).strip()}
        if allowed is not None:
            tools = [tool for tool in tools if tool.name in allowed]
            logger.info(
                "[%s] Plan-Solve branch tools restricted to: %s",
                cfg.label,
                sorted(allowed),
            )
        disallowed: set[str] = set()
        if (
            cfg.request_metadata
            and "coordination_disallowed_tools" in cfg.request_metadata
        ):
            raw_disallowed = (
                cfg.request_metadata.get("coordination_disallowed_tools") or []
            )
            disallowed = {
                str(name).strip() for name in raw_disallowed if str(name).strip()
            }
        if disallowed:
            tools = [tool for tool in tools if tool.name not in disallowed]
            logger.info(
                "[%s] Coordination branch tools excluded: %s",
                cfg.label,
                sorted(disallowed),
            )
        return tools

    def _create_summarization_middleware(
        self, context_policy: Any | None = None
    ) -> Any:
        from langchain.agents.middleware import SummarizationMiddleware

        from backend.core.agents.context import ContextPolicy
        from backend.core.models import create_chat_model

        policy = context_policy or ContextPolicy.from_env(self._config.model_name)

        summary_model_name = (
            os.getenv("AGENT_SUMMARIZATION_MODEL", "").strip()
            or self._config.model_name
            or None
        )
        sum_model = create_chat_model(name=summary_model_name, thinking_enabled=False)
        sum_model = sum_model.with_config(
            tags=["summarization"],
            metadata={"purpose": "summarization"},
        )
        return SummarizationMiddleware(
            model=sum_model,
            trigger=("tokens", policy.history_summary_trigger_tokens),
            keep=("tokens", policy.history_summary_keep_tokens),
        )
