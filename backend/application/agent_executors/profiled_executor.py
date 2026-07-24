"""Profile-driven agent executor facade."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from backend.application.agent_executors.graph_builder import (
    ProfiledGraphBuildConfig,
    ProfiledGraphBuilder,
)
from backend.application.agent_executors.streaming_runtime import (
    AgentStreamRuntime,
    StreamRuntimeConfig,
)
from backend.application.agents.definition import ResolvedAgentDefinition
from backend.application.tools.assembler import PolicyToolAssembler
from backend.core.agents.context import ContextPolicy
from backend.core.agents.control import RunBudget
from backend.core.skills.scoped_skills_provider import ScopedSkillsProvider
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.runtime_agent import RuntimeAgentSpec
from backend.domain.agents.runtime_bindings import AgentRuntimeBindings
from backend.domain.agents.runtime_budget import AgentRuntimeBudget

logger = logging.getLogger(__name__)
_ARTIFACT_METADATA_TIMEOUT_SECONDS = 10.0
_REPORT_SAVE_TIMEOUT_SECONDS = 120.0


def _run_budget_from_runtime_budget(
    budget: AgentRuntimeBudget,
    *,
    max_active_agents: int,
    request_deadline_owned_by_stream: bool = False,
) -> RunBudget:
    """Translate the application budget into the shared tree budget once."""

    max_spawned_agents = max(
        16,
        int(budget.plan_solve_max_concurrent_subagents) + 4,
    )
    active = max(1, min(int(max_active_agents), max_spawned_agents))
    return RunBudget(
        max_spawned_agents=max_spawned_agents,
        max_active_agents=active,
        max_depth=1,
        max_tokens=budget.tree_max_tokens,
        max_tool_calls=budget.tree_max_tool_calls,
        max_elapsed_seconds=(
            None
            if request_deadline_owned_by_stream
            else float(budget.workflow_total_timeout_seconds)
        ),
    )


class ProfiledAgentExecutor:
    """Application executor contract backed directly by profile runtime config."""

    DEEP_RESEARCH_TASK_TYPE = "deep_research"
    DEEP_RESEARCH_STREAM_TIMEOUT_SECONDS = 1800
    DEEP_RESEARCH_IDLE_TIMEOUT_SECONDS = 600
    DEEP_RESEARCH_FINALIZE_IDLE_TIMEOUT_SECONDS = 300
    DEEP_RESEARCH_TASK_STALL_GRACE_SECONDS = 300
    DEEP_RESEARCH_FINALIZE_GRACE_SECONDS = 180

    def __init__(self, *, profile: AgentProfile, **kwargs: Any):
        self.profile = profile
        self._resolved_agent_definition: ResolvedAgentDefinition | None = kwargs.pop(
            "resolved_agent_definition",
            None,
        )
        if self._resolved_agent_definition is not None:
            if not isinstance(
                self._resolved_agent_definition,
                ResolvedAgentDefinition,
            ):
                raise TypeError(
                    "resolved_agent_definition must be a ResolvedAgentDefinition instance"
                )
            if self._resolved_agent_definition.profile.code != profile.code:
                raise ValueError(
                    "Resolved definition agent_code does not match executor profile: "
                    f"{self._resolved_agent_definition.profile.code} != {profile.code}"
                )
        registry_scope = (
            self._resolved_agent_definition.runtime_scope
            if self._resolved_agent_definition is not None
            else None
        )
        self._prompt_provider = (
            self._resolved_agent_definition.prompt_provider
            if self._resolved_agent_definition is not None
            else None
        )
        self._policy_tool_assembler = PolicyToolAssembler(
            registry=(
                registry_scope.tool_registry if registry_scope is not None else None
            )
        )
        self._agent_bindings: AgentRuntimeBindings | None = kwargs.pop(
            "agent_bindings", None
        )
        if self._agent_bindings is not None:
            if not isinstance(self._agent_bindings, AgentRuntimeBindings):
                raise TypeError(
                    "agent_bindings must be an AgentRuntimeBindings instance"
                )
            if self._agent_bindings.agent_code != profile.code:
                raise ValueError(
                    "Runtime binding agent_code does not match executor profile: "
                    f"{self._agent_bindings.agent_code} != {profile.code}"
                )
        self._model_name: str | None = kwargs.pop("model_name", None)
        self._thinking_enabled = bool(kwargs.pop("thinking_enabled", False))
        self._subagent_enabled = bool(kwargs.pop("subagent_enabled", False))
        self._use_search = bool(kwargs.pop("use_search", True))
        self._lightweight_mode = bool(kwargs.pop("lightweight_mode", False))
        self._thread_id: str | None = kwargs.pop("thread_id", None)
        self._external_thread_id: str | None = kwargs.pop(
            "external_thread_id", self._thread_id
        )
        self._request_metadata = dict(kwargs.pop("request_metadata", None) or {})
        self._workflow_name = str(kwargs.pop("workflow_name", "") or "").strip() or None
        requested_domain = kwargs.pop("domain", profile.domain)
        requested_prompt_profile = kwargs.pop("prompt_profile", profile.prompt_profile)
        requested_tool_policy = kwargs.pop("tool_policy", profile.tool_policy)
        self._domain = str(
            self._agent_bindings.domain
            if self._agent_bindings is not None
            else requested_domain or profile.domain
        )
        self._prompt_profile = str(
            self._agent_bindings.prompt_profile
            if self._agent_bindings is not None
            else requested_prompt_profile or profile.prompt_profile
        )
        self._tool_policy = str(
            self._agent_bindings.tool_policy
            if self._agent_bindings is not None
            else requested_tool_policy or profile.tool_policy
        )
        self._guardrail_policy = str(
            kwargs.pop("guardrail_policy", profile.guardrail_policy)
            or profile.guardrail_policy
        )
        self._runtime_permissions = dict(kwargs.pop("runtime_permissions", None) or {})
        self._guardrail_instructions = str(
            kwargs.pop("guardrail_instructions", "") or ""
        )
        self._time_context = str(kwargs.pop("time_context", "") or "")
        self._tool_runtime_context = kwargs.pop("tool_runtime_context", None)
        self._task_type = str(kwargs.pop("task_type", "") or "").strip() or None
        self._checkpointer = kwargs.pop("checkpointer", None)
        self._agent_runtime_budget: AgentRuntimeBudget | None = kwargs.pop(
            "runtime_budget", None
        )
        if self._agent_runtime_budget is not None and not isinstance(
            self._agent_runtime_budget, AgentRuntimeBudget
        ):
            raise TypeError("runtime_budget must be an AgentRuntimeBudget instance")
        self._agent_specs: dict[str, RuntimeAgentSpec] = dict(
            kwargs.pop("agent_specs", None) or {}
        )
        self._context_policy: ContextPolicy | None = kwargs.pop("context_policy", None)
        self._run_budget: RunBudget | None = kwargs.pop("run_budget", None)
        if self._run_budget is not None and not isinstance(self._run_budget, RunBudget):
            raise TypeError("run_budget must be a RunBudget instance")
        self._user_id = str(kwargs.pop("user_id", "") or "").strip() or None
        self._managed_skills = bool(kwargs.pop("managed_skills", False))
        self._skills_provider = kwargs.pop(
            "skills_provider", None
        ) or ScopedSkillsProvider(user_id=self._user_id)
        supplied_kb_ids = kwargs.pop("kb_ids", None)
        self._kb_ids = list(
            profile.default_kb_ids if supplied_kb_ids is None else supplied_kb_ids
        )
        self._selected_skills = self._normalize_selected_skills(
            kwargs.pop("selected_skills", None)
        )
        self._lightweight_has_kb = self._lightweight_mode and bool(self._kb_ids)
        self._max_concurrent_subagents = int(
            kwargs.pop("max_concurrent_subagents", 5) or 5
        )
        if self._run_budget is None and self._agent_runtime_budget is not None:
            self._run_budget = _run_budget_from_runtime_budget(
                self._agent_runtime_budget,
                max_active_agents=self._max_concurrent_subagents,
                request_deadline_owned_by_stream=(
                    self._task_type == self.DEEP_RESEARCH_TASK_TYPE
                    or self._workflow_name == "deep_research"
                ),
            )
        self._normal_max_output_chars = int(
            kwargs.pop("normal_max_output_chars", 0) or 0
        )
        self._multi_kb_search_max_calls = int(
            kwargs.pop(
                "multi_kb_search_max_calls", self._default_multi_kb_search_max_calls()
            )
            or self._default_multi_kb_search_max_calls()
        )
        if kwargs:
            raise TypeError(
                "Unsupported profiled executor arguments: " + ", ".join(sorted(kwargs))
            )

        self._validate_profile_bindings()

        runtime_agent_name = str(
            self._request_metadata.get("runtime_agent") or ""
        ).strip()
        coordination_role = str(
            self._request_metadata.get("coordination_role") or ""
        ).strip()
        runtime_agent_spec = (
            self._agent_specs.get(runtime_agent_name)
            if runtime_agent_name and runtime_agent_name == coordination_role
            else None
        )

        graph_result = ProfiledGraphBuilder(
            ProfiledGraphBuildConfig(
                label=self._label,
                graph_name=self._graph_name,
                domain=self._domain,
                tool_policy=self._tool_policy,
                guardrail_policy=self._guardrail_policy,
                runtime_permissions=self._runtime_permissions,
                guardrail_instructions=self._guardrail_instructions,
                time_context=self._time_context,
                prompt_profile=self._prompt_profile,
                workflow_name=self._workflow_name,
                model_name=self._model_name,
                thinking_enabled=self._thinking_enabled,
                subagent_enabled=self._subagent_enabled,
                use_search=self._use_search,
                kb_ids=list(self._kb_ids),
                selected_skills=self._selected_skills,
                skills_provider=self._skills_provider,
                task_type=self._task_type,
                lightweight_mode=self._lightweight_mode,
                lightweight_has_kb=self._lightweight_has_kb,
                thread_id=self._thread_id,
                external_thread_id=self._external_thread_id,
                max_concurrent_subagents=self._max_concurrent_subagents,
                checkpointer=self._checkpointer,
                deep_research_task_type=self.DEEP_RESEARCH_TASK_TYPE,
                tool_runtime_context=self._tool_runtime_context,
                request_metadata={
                    **dict(self._request_metadata),
                    "_tool_policy": self._tool_policy,
                    "_agent_code": self.profile.code,
                    "_domain": self._domain,
                    "_domain_context": dict(
                        getattr(self._tool_runtime_context, "domain_context", {}) or {}
                    ),
                },
                context_policy=self._context_policy,
                runtime_agent_spec=runtime_agent_spec,
                prompt_provider=self._prompt_provider,
                policy_tool_assembler=self._policy_tool_assembler,
            )
        ).build()
        self._graph = graph_result.graph
        self._model_name = graph_result.model_name
        self._compaction_checkpoints = getattr(
            graph_result,
            "compaction_checkpoints",
            None,
        )
        self._runtime_tools = {
            str(getattr(tool, "name", "") or ""): tool
            for tool in graph_result.tools
            if str(getattr(tool, "name", "") or "")
        }

    @property
    def graph(self):
        """Expose the underlying compiled LangGraph for state operations (e.g. rollback)."""
        return self._graph

    @property
    def compaction_checkpoints(self):
        """Expose checkpoint-aware compaction recovery for this executor."""

        return self._compaction_checkpoints

    async def arecover_compaction_checkpoint(
        self,
        *,
        thread_id: str | None = None,
        checkpoint_id: str | None = None,
    ):
        """Recover the latest or specified compacted context checkpoint."""

        if self._compaction_checkpoints is None:
            return None
        return await self._compaction_checkpoints.arecover(
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
        )

    @property
    def _domain_key(self) -> str:
        # ui_profile is presentation metadata and must never select runtime
        # semantics. The stable agent code is the runtime identity; domain is
        # retained only for compatibility with directly constructed profiles.
        raw = str(self.profile.code or self._domain or "").strip().lower()
        if not raw:
            raise ValueError("Profiled agent requires a non-empty code or domain")
        if raw in {"csh", "csh-hydrogeology", "hydrogeology"}:
            return "csh"
        if raw in {"groundwater", "groundwater-project-analysis"}:
            return "groundwater"
        if raw in {"ne", "nuclear-electronics"}:
            return "ne"
        return raw

    @property
    def _label(self) -> str:
        labels = {
            "ne": "ProfiledNEAgent",
            "csh": "ProfiledCSHAgent",
            "groundwater": "ProfiledGroundwaterAgent",
        }
        return labels.get(
            self._domain_key,
            f"ProfiledAgent[{self.profile.code}]",
        )

    @property
    def _graph_name(self) -> str:
        names = {
            "ne": "ne-full-feature",
            "csh": "csh-full-feature",
            "groundwater": "groundwater-full-feature",
        }
        return names.get(
            self._domain_key,
            f"{self._domain_key}-full-feature",
        )

    def _validate_profile_bindings(self) -> None:
        required = {
            "domain": self._domain,
            "prompt_profile": self._prompt_profile,
            "tool_policy": self._tool_policy,
            "guardrail_policy": self._guardrail_policy,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(
                f"Agent profile '{self.profile.code}' is missing explicit runtime "
                f"bindings: {', '.join(missing)}"
            )
        placeholders = [
            name
            for name in ("prompt_profile", "tool_policy")
            if str(required[name]).strip().lower() == "default"
        ]
        if placeholders:
            raise ValueError(
                f"Agent profile '{self.profile.code}' must replace placeholder "
                f"bindings with registered names: {', '.join(placeholders)}"
            )

    def _default_multi_kb_search_max_calls(self) -> int:
        if self._domain_key == "csh":
            # Each public tool call performs exactly one provider retrieval;
            # keep one optional second call for a distinct evidence gap.
            return 2
        if self._domain_key == "groundwater":
            return 3
        return 2

    def _normalize_selected_skills(
        self, selected_skills: list[str] | None
    ) -> set[str] | None:
        raw_skills = (
            self.profile.default_skills if selected_skills is None else selected_skills
        )
        normalized = {
            str(skill).strip()
            for skill in (raw_skills or [])
            if isinstance(skill, str) and str(skill).strip()
        }
        if self._task_type == self.DEEP_RESEARCH_TASK_TYPE and (
            not self._managed_skills or "deep-research" in normalized
        ):
            normalized.add("deep-research")
        return normalized

    async def aquery_stream(
        self,
        question: str,
        thread_id: str | None = None,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ):
        runtime = AgentStreamRuntime(
            StreamRuntimeConfig(
                label=self._label,
                graph=self._graph,
                model_name=self._model_name or "",
                user_id=self._user_id,
                kb_ids=list(self._kb_ids),
                use_search=self._use_search,
                subagent_enabled=self._subagent_enabled,
                max_concurrent_subagents=self._max_concurrent_subagents,
                selected_skills=self._selected_skills,
                task_type=self._task_type,
                lightweight_mode=self._lightweight_mode,
                lightweight_thread_id=self._thread_id,
                lightweight_external_thread_id=self._external_thread_id,
                thinking_enabled=self._thinking_enabled,
                prompt_domain=self._domain_key,
                prompt_profile=self._prompt_profile,
                prompt_provider=self._prompt_provider,
                guardrail_policy=self._guardrail_policy,
                runtime_permissions=dict(self._runtime_permissions),
                guardrail_instructions=self._guardrail_instructions,
                time_context=self._time_context,
                lightweight_allowed_tools=self._resolve_lightweight_allowed_tools(),
                runtime_tools=dict(self._runtime_tools),
                request_metadata=dict(self._request_metadata),
                runtime_agent_specs=dict(self._agent_specs),
                normal_max_output_chars=self._normal_max_output_chars,
                multi_kb_search_max_calls=self._multi_kb_search_max_calls,
                deep_research_task_type=self.DEEP_RESEARCH_TASK_TYPE,
                deep_research_stream_timeout_seconds=self.DEEP_RESEARCH_STREAM_TIMEOUT_SECONDS,
                deep_research_idle_timeout_seconds=self.DEEP_RESEARCH_IDLE_TIMEOUT_SECONDS,
                deep_research_finalize_idle_timeout_seconds=self.DEEP_RESEARCH_FINALIZE_IDLE_TIMEOUT_SECONDS,
                deep_research_task_stall_grace_seconds=self.DEEP_RESEARCH_TASK_STALL_GRACE_SECONDS,
                deep_research_finalize_grace_seconds=self.DEEP_RESEARCH_FINALIZE_GRACE_SECONDS,
                emit_graph_artifacts=self._emit_graph_artifacts,
                finalize_deep_research_report=self._finalize_deep_research_report,
                resolve_history_image_tokens=self._resolve_history_image_tokens,
                warn_low_visible_output=self._domain_key == "ne",
                run_budget=self._run_budget,
                context_policy=self._context_policy,
                compaction_checkpoints=self._compaction_checkpoints,
                checkpointer=self._checkpointer,
            )
        )
        async for event in runtime.aquery_stream(
            question=question,
            thread_id=thread_id,
            config=config,
            image_contents=kwargs.get("image_contents"),
        ):
            yield event

    def _resolve_lightweight_allowed_tools(self) -> list[str]:
        if not self._lightweight_mode:
            return []
        if self._runtime_tools:
            return sorted(self._runtime_tools)
        from backend.application.tools.assembler import ToolAssemblyContext

        specs = self._policy_tool_assembler.resolve_specs(
            ToolAssemblyContext(
                domain=self._domain,
                workflow=self._workflow_name or "lightweight",
                policy_name=self._tool_policy,
                selected_skills=self._selected_skills or set(),
            )
        )
        return [spec.name for spec in specs]

    async def ainvoke(
        self,
        question: str,
        thread_id: str | None = None,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ):
        result = ""
        async for event in self.aquery_stream(question, thread_id, config, **kwargs):
            if event.get("type") == "chunk":
                result += event.get("content", "")
        return result

    async def _emit_graph_artifacts(
        self,
        *,
        effective_config: RunnableConfig,
        effective_thread_id: str,
        handler: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        from backend.application.agent_executors.stream_utils import make_process_event
        from backend.application.artifacts.artifact_utils import (
            build_registered_artifact_metadata,
        )

        try:
            state = await asyncio.wait_for(
                self._graph.aget_state(effective_config),
                timeout=2.0,
            )
            artifacts = state.values.get("artifacts", [])
            for art in artifacts:
                if isinstance(art, str):
                    art_obj = {
                        "id": art,
                        "title": os.path.basename(art) or "新工件",
                        "type": "file",
                    }
                elif isinstance(art, dict):
                    art_obj = art
                else:
                    art_obj = {
                        "id": str(
                            hash(json.dumps(art, ensure_ascii=False, default=str))
                        ),
                        "title": "新工件",
                        "type": "file",
                    }

                if (
                    isinstance(art, str)
                    and getattr(handler, "_is_deep_research", False)
                    and art.lower().endswith(".docx")
                ):
                    continue

                art_id = art_obj.get("id") or str(
                    hash(json.dumps(art_obj, ensure_ascii=False))
                )
                if art_id in handler.sent_artifacts:
                    continue

                handler.sent_artifacts.add(art_id)
                safe_art_obj = dict(art_obj)
                safe_art_obj.pop("path", None)
                safe_art_obj.pop("report_url", None)

                if safe_art_obj.get("type") == "file" and isinstance(art, str):
                    try:
                        registered = await asyncio.wait_for(
                            build_registered_artifact_metadata(
                                internal_thread_id=effective_thread_id,
                                artifact_path=art,
                                title=str(safe_art_obj.get("title") or "新工件"),
                            ),
                            timeout=_ARTIFACT_METADATA_TIMEOUT_SECONDS,
                        )
                        safe_art_obj.update(registered)
                    except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041
                        logger.warning(
                            "[%s] 工件元数据注册超时，已降级返回基础信息",
                            self._label,
                        )

                yield {
                    "type": "event",
                    "event": make_process_event(
                        kind="artifact",
                        title=safe_art_obj.get("title", "新工件"),
                        detail="已生成工件，可通过服务器中转安全下载/预览",
                        level="success",
                        metadata=safe_art_obj,
                    ),
                }
        # Python 3.10 keeps asyncio.TimeoutError distinct from builtin TimeoutError.
        except asyncio.TimeoutError:  # noqa: UP041
            logger.warning("[%s] 读取图状态超时，已跳过本次工件轮询", self._label)
        except Exception:
            logger.warning(
                "[%s] 读取图状态用于工件轮询失败",
                self._label,
                exc_info=True,
            )

    async def _finalize_deep_research_report(
        self,
        *,
        question: str,
        effective_thread_id: str,
        handler: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        from backend.application.agent_executors.image_url_registry import (
            resolve_image_tokens,
        )
        from backend.application.agent_executors.stream_utils import make_process_event
        from backend.application.artifacts.artifact_utils import (
            build_registered_artifact_metadata,
        )
        from backend.application.image_pipeline import sanitize_answer_image_urls
        from backend.application.tools.runtime_invoker import (
            RuntimeToolInvoker,
            parse_tool_json_result,
        )
        from backend.domain.agents.runtime_context import AgentRuntimeContext

        yield {
            "type": "status",
            "content": "正在执行最终归档并保存研究报告...",
        }

        report_title = "深度研究报告"
        answer_text = sanitize_answer_image_urls(
            resolve_image_tokens("".join(handler.answer_fragments).strip())
        )
        report_content = handler.build_live_markdown_content(
            result_content=(
                answer_text
                if answer_text
                else "本轮未形成完整自然语言结论，已先归档阶段性结果。"
            ),
            title=report_title,
        )

        yield {
            "type": "event",
            "event": make_process_event(
                kind="artifact_update",
                title="深度研究报告（最终预览）",
                detail="已生成最终报告内容，正在转换 DOCX",
                level="info",
                metadata=handler.build_live_markdown_artifact(
                    content=report_content,
                    title="深度研究报告（最终预览）",
                    is_streaming=False,
                ),
            ),
        }

        runtime_context = AgentRuntimeContext(
            request_id=str(self._request_metadata.get("_request_id") or ""),
            user_id=str(self._user_id or ""),
            external_thread_id=str(self._external_thread_id or effective_thread_id),
            internal_thread_id=effective_thread_id,
            agent_profile=self.profile,
            workflow_name="deep_research",
            model_name=self._model_name,
            kb_ids=list(self._kb_ids),
            selected_skills=sorted(self._selected_skills or []),
            use_search=self._use_search,
            subagent_enabled=self._subagent_enabled,
            thinking_enabled=self._thinking_enabled,
            permissions=dict(self._runtime_permissions),
            guardrail_instructions=self._guardrail_instructions,
            time_context=self._time_context,
            request_metadata=dict(self._request_metadata),
            domain_context=dict(
                getattr(self._tool_runtime_context, "domain_context", {}) or {}
            ),
            runtime_budget=getattr(self, "_agent_runtime_budget", None),
            agent_specs=dict(getattr(self, "_agent_specs", {}) or {}),
            context_policy=getattr(self, "_context_policy", None),
        )
        report_payload = parse_tool_json_result(
            await asyncio.wait_for(
                RuntimeToolInvoker(
                    context=runtime_context,
                    workflow_name="deep_research",
                    enforce_skill_load=False,
                    resolved_agent_definition=getattr(
                        self,
                        "_resolved_agent_definition",
                        None,
                    ),
                ).invoke(
                    "save_deep_research_report",
                    {"title": report_title, "content": report_content},
                ),
                timeout=_REPORT_SAVE_TIMEOUT_SECONDS,
            )
        )
        artifact_path = str(report_payload.get("artifact_path") or "")
        if not artifact_path:
            return

        handler.report_saved = True
        artifact_title = str(report_payload.get("title") or "深度研究报告")
        try:
            artifact_metadata = await asyncio.wait_for(
                build_registered_artifact_metadata(
                    internal_thread_id=effective_thread_id,
                    artifact_path=artifact_path,
                    title=artifact_title,
                    minio_object=str(report_payload.get("minio_object") or "") or None,
                ),
                timeout=_ARTIFACT_METADATA_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041
            logger.warning(
                "[%s] 最终报告元数据注册超时，已降级返回工具结果",
                self._label,
            )
            artifact_metadata = dict(report_payload)
        if not answer_text:
            fallback_answer = (
                "主流程未返回完整结论，系统已根据已收集证据完成研究报告归档。"
            )
            handler.answer_fragments.append(fallback_answer)
            handler.chunk_count += 1
            try:
                await asyncio.wait_for(
                    self._graph.aupdate_state(
                        {"configurable": {"thread_id": effective_thread_id}},
                        {"messages": [AIMessage(content=fallback_answer)]},
                        as_node="model",
                    ),
                    timeout=3.0,
                )
            except Exception:
                logger.warning(
                    "[%s] 深度研究兜底答案写入历史失败", self._label, exc_info=True
                )
            yield {
                "type": "chunk",
                "content": fallback_answer,
            }

        yield {
            "type": "event",
            "event": make_process_event(
                kind="tool_end",
                title="研究报告已保存",
                detail="报告已归档并生成服务器中转下载地址",
                tool="save_deep_research_report",
                level="success",
                metadata={
                    **report_payload,
                    "save_mode": "system_finalize",
                },
            ),
        }
        yield {
            "type": "event",
            "event": make_process_event(
                kind="artifact",
                title=artifact_title,
                detail="已生成工件，可通过服务器中转安全下载/预览",
                level="success",
                metadata=artifact_metadata,
            ),
        }

    async def _resolve_history_image_tokens(
        self, effective_config: RunnableConfig
    ) -> None:
        from langchain_core.messages import AIMessage as _AIMessage
        from langchain_core.messages import ToolMessage as _ToolMessage

        from backend.application.agent_executors.stream_utils import (
            stringify_message_content,
        )
        from backend.application.image_pipeline import strip_unresolved_image_tokens

        try:
            state = await asyncio.wait_for(
                self._graph.aget_state(effective_config),
                timeout=2.0,
            )
        except Exception as exc:
            logger.debug(
                "[%s] 读取图状态用于 image token 解析失败: %s", self._label, exc
            )
            return

        if not state:
            return

        messages = state.values.get("messages") or []
        messages_to_update: list[Any] = []

        for msg in reversed(messages):
            if isinstance(msg, _AIMessage):
                raw = stringify_message_content(msg.content)
                if "[IMAGE:" not in raw:
                    break
                cleaned = strip_unresolved_image_tokens(raw)
                if cleaned != raw:
                    messages_to_update.append(
                        msg.model_copy(update={"content": cleaned})
                    )
            elif isinstance(msg, _ToolMessage):
                raw = stringify_message_content(msg.content)
                if "[IMAGE:" not in raw:
                    continue
                cleaned = strip_unresolved_image_tokens(raw)
                messages_to_update.append(msg.model_copy(update={"content": cleaned}))

        if not messages_to_update:
            return

        try:
            await asyncio.wait_for(
                self._graph.aupdate_state(
                    effective_config,
                    {"messages": messages_to_update},
                ),
                timeout=3.0,
            )
            logger.info(
                "[%s] 已清理历史中 %d 条消息的图片 token",
                self._label,
                len(messages_to_update),
            )
        except Exception as exc:
            logger.warning(
                "[%s] 清理历史图片 token 失败（不影响本次回答）: %s", self._label, exc
            )
