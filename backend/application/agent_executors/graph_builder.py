"""Platform LangGraph builder for profiled full-feature agents."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from backend.application.agents.prompt_builder import PromptBuilder, PromptBuildContext
from backend.core.skills.scoped_skills_provider import ScopedSkillsProvider

logger = logging.getLogger(__name__)


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
    max_concurrent_subagents: int
    checkpointer: BaseCheckpointSaver | None
    guardrail_policy: str = "default"
    prompt_profile: str | None = None
    deep_research_task_type: str = "deep_research"
    tool_runtime_context: Any | None = None
    runtime_permissions: dict[str, Any] | None = None
    guardrail_instructions: str = ""
    time_context: str = ""


@dataclass(frozen=True)
class ProfiledGraphBuildResult:
    graph: CompiledStateGraph
    model_name: str | None
    tools: list[Any]


class ProfiledGraphBuilder:
    """Build LangGraph graphs from normalized agent profile/runtime config."""

    def __init__(self, config: ProfiledGraphBuildConfig):
        self._config = config

    def build(self) -> ProfiledGraphBuildResult:
        from backend.core.agents import RuntimeFeatures, ThreadState, create_agent_graph
        from backend.core.agents.config_compat import get_default_model_name
        from backend.core.agents.middlewares.context_pruning import ContextPruningMiddleware
        from backend.core.agents.middlewares.runtime_policy import RuntimePolicyGuardrailProvider
        from backend.core.agents.middlewares.subagent_limit import SubagentLimitMiddleware
        from backend.core.agents.middlewares.tool_guardrail import ToolGuardrailMiddleware
        from backend.core.models import create_chat_model

        cfg = self._config
        model = create_chat_model(
            name=cfg.model_name,
            thinking_enabled=cfg.thinking_enabled,
        )
        effective_model_name = cfg.model_name
        if effective_model_name is None:
            default_name = get_default_model_name()
            if default_name:
                effective_model_name = default_name
                logger.info("[%s] 使用默认模型: %s", cfg.label, effective_model_name)

        logger.info("[%s] 创建模型: %s", cfg.label, type(model).__name__)

        tools = self._assemble_tools(model_name=effective_model_name)
        logger.info("[%s] 工具列表: %s", cfg.label, [tool.name for tool in tools])

        guardrail_mw = ToolGuardrailMiddleware(
            provider=RuntimePolicyGuardrailProvider(
                allowed_tools=[tool.name for tool in tools],
                guardrail_policy=cfg.guardrail_policy,
                permissions=dict(cfg.runtime_permissions or {}),
            ),
            passport=cfg.guardrail_policy,
        )

        subagent_feature: bool | Any = False
        if cfg.subagent_enabled:
            subagent_feature = SubagentLimitMiddleware(max_concurrent=cfg.max_concurrent_subagents)

        features = RuntimeFeatures(
            summarization=self._create_summarization_middleware(),
            vision=True,
            subagent=subagent_feature,
            clarification=False,
            guardrail=guardrail_mw,
        )

        graph = create_agent_graph(
            model=model,
            tools=tools,
            system_prompt=self._build_system_prompt(),
            features=features,
            extra_middleware=[ContextPruningMiddleware(char_threshold=1500, keep_latest_n=1)],
            checkpointer=cfg.checkpointer,
            name=cfg.graph_name,
            state_schema=ThreadState,
        )
        logger.info("[%s] 图构建完成: %s", cfg.label, graph.name)
        return ProfiledGraphBuildResult(
            graph=graph,
            model_name=effective_model_name,
            tools=tools,
        )

    def _build_system_prompt(self) -> str:
        cfg = self._config
        if cfg.lightweight_mode:
            return f"{cfg.time_context}{cfg.guardrail_instructions}"
        return PromptBuilder(skills_provider=cfg.skills_provider).build(
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
            )
        )

    def _assemble_tools(self, *, model_name: str | None) -> list[Any]:
        from backend.application.tools.tool_assembly_pipeline import ToolAssemblyPipeline

        cfg = self._config
        workflow_name = cfg.workflow_name or (
            "lightweight" if cfg.lightweight_mode else cfg.task_type or "full"
        )
        return ToolAssemblyPipeline(
            model_name=model_name,
            subagent_enabled=cfg.subagent_enabled,
            use_search=cfg.use_search,
            kb_ids=list(cfg.kb_ids),
            selected_skills=cfg.selected_skills,
            skills_provider=cfg.skills_provider,
            task_type=cfg.task_type,
            lightweight_mode=cfg.lightweight_mode,
            thread_id=cfg.thread_id,
            domain=cfg.domain,
            workflow_name=workflow_name,
            tool_policy=cfg.tool_policy,
            tool_runtime_context=cfg.tool_runtime_context,
        ).assemble()

    def _create_summarization_middleware(self) -> Any:
        from langchain.agents.middleware import SummarizationMiddleware

        from backend.core.models import create_chat_model

        default_model = get_default_model_name()
        sum_model = create_chat_model(name=default_model, thinking_enabled=False)
        sum_model = sum_model.with_config(
            tags=["summarization"],
            metadata={"purpose": "summarization"},
        )
        return SummarizationMiddleware(
            model=sum_model,
            trigger=[("messages", 20)],
            keep=("messages", 20),
        )
