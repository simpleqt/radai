"""Profile-driven agent executor facade."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from backend.application.agent_executors.graph_builder import (
    ProfiledGraphBuilder,
    ProfiledGraphBuildConfig,
)
from backend.application.agent_executors.streaming_runtime import (
    AgentStreamRuntime,
    StreamRuntimeConfig,
)
from backend.core.skills.scoped_skills_provider import ScopedSkillsProvider
from backend.domain.agents.profile import AgentProfile

logger = logging.getLogger(__name__)


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
        self._model_name: str | None = kwargs.pop("model_name", None)
        self._thinking_enabled = bool(kwargs.pop("thinking_enabled", False))
        self._subagent_enabled = bool(kwargs.pop("subagent_enabled", False))
        self._use_search = bool(kwargs.pop("use_search", True))
        self._lightweight_mode = bool(kwargs.pop("lightweight_mode", False))
        self._thread_id: str | None = kwargs.pop("thread_id", None)
        self._workflow_name = str(kwargs.pop("workflow_name", "") or "").strip() or None
        self._domain = str(kwargs.pop("domain", profile.domain) or profile.domain)
        self._prompt_profile = str(
            kwargs.pop("prompt_profile", profile.prompt_profile) or profile.prompt_profile
        )
        self._tool_policy = str(kwargs.pop("tool_policy", profile.tool_policy) or profile.tool_policy)
        self._guardrail_policy = str(
            kwargs.pop("guardrail_policy", profile.guardrail_policy) or profile.guardrail_policy
        )
        self._runtime_permissions = dict(kwargs.pop("runtime_permissions", None) or {})
        self._guardrail_instructions = str(kwargs.pop("guardrail_instructions", "") or "")
        self._time_context = str(kwargs.pop("time_context", "") or "")
        self._tool_runtime_context = kwargs.pop("tool_runtime_context", None)
        self._task_type = str(kwargs.pop("task_type", "") or "").strip() or None
        self._checkpointer = kwargs.pop("checkpointer", None)
        self._user_id = str(kwargs.pop("user_id", "") or "").strip() or None
        self._skills_provider = kwargs.pop("skills_provider", None) or ScopedSkillsProvider(
            user_id=self._user_id
        )
        supplied_kb_ids = kwargs.pop("kb_ids", None)
        self._kb_ids = list(
            profile.default_kb_ids if supplied_kb_ids is None else supplied_kb_ids
        )
        self._selected_skills = self._normalize_selected_skills(
            kwargs.pop("selected_skills", None)
        )
        self._lightweight_has_kb = self._lightweight_mode and bool(self._kb_ids)
        self._max_concurrent_subagents = int(kwargs.pop("max_concurrent_subagents", 3) or 3)
        self._normal_max_output_chars = int(kwargs.pop("normal_max_output_chars", 0) or 0)
        self._multi_kb_search_max_calls = int(
            kwargs.pop("multi_kb_search_max_calls", self._default_multi_kb_search_max_calls())
            or self._default_multi_kb_search_max_calls()
        )
        if kwargs:
            raise TypeError(
                "Unsupported profiled executor arguments: "
                + ", ".join(sorted(kwargs))
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
                max_concurrent_subagents=self._max_concurrent_subagents,
                checkpointer=self._checkpointer,
                deep_research_task_type=self.DEEP_RESEARCH_TASK_TYPE,
                tool_runtime_context=self._tool_runtime_context,
            )
        ).build()
        self._graph = graph_result.graph
        self._model_name = graph_result.model_name

    @property
    def graph(self):
        """Expose the underlying compiled LangGraph for state operations (e.g. rollback)."""
        return self._graph

    @property
    def _domain_key(self) -> str:
        raw = str(self.profile.ui_profile or self.profile.code or self._domain or "ne").strip().lower()
        if raw in {"csh", "csh-hydrogeology", "hydrogeology"}:
            return "csh"
        if raw in {"groundwater", "groundwater-project-analysis"}:
            return "groundwater"
        return "ne"

    @property
    def _label(self) -> str:
        labels = {
            "csh": "ProfiledCSHAgent",
            "groundwater": "ProfiledGroundwaterAgent",
        }
        return labels.get(self._domain_key, "ProfiledNEAgent")

    @property
    def _graph_name(self) -> str:
        names = {
            "csh": "csh-full-feature",
            "groundwater": "groundwater-full-feature",
        }
        return names.get(self._domain_key, "ne-full-feature")

    def _default_multi_kb_search_max_calls(self) -> int:
        if self._domain_key == "csh":
            return 5
        if self._domain_key == "groundwater":
            return 3
        return 2

    def _normalize_selected_skills(self, selected_skills: list[str] | None) -> set[str] | None:
        if self._lightweight_mode:
            return set()
        raw_skills = self.profile.default_skills if selected_skills is None else selected_skills
        normalized = {
            str(skill).strip()
            for skill in (raw_skills or [])
            if isinstance(skill, str) and str(skill).strip()
        }
        if self._task_type == self.DEEP_RESEARCH_TASK_TYPE:
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
                kb_ids=list(self._kb_ids),
                use_search=self._use_search,
                subagent_enabled=self._subagent_enabled,
                max_concurrent_subagents=self._max_concurrent_subagents,
                selected_skills=self._selected_skills,
                task_type=self._task_type,
                lightweight_mode=self._lightweight_mode,
                lightweight_thread_id=self._thread_id,
                thinking_enabled=self._thinking_enabled,
                prompt_domain=self._domain_key,
                guardrail_policy=self._guardrail_policy,
                runtime_permissions=dict(self._runtime_permissions),
                guardrail_instructions=self._guardrail_instructions,
                time_context=self._time_context,
                lightweight_allowed_tools=self._resolve_lightweight_allowed_tools(),
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
        from backend.application.tools.assembler import (
            PolicyToolAssembler,
            ToolAssemblyContext,
        )

        specs = PolicyToolAssembler().resolve_specs(
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
        from backend.application.artifacts.artifact_utils import (
            build_registered_artifact_metadata,
        )
        from backend.application.agent_executors.stream_utils import make_process_event

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
                        "id": str(hash(json.dumps(art, ensure_ascii=False, default=str))),
                        "title": "新工件",
                        "type": "file",
                    }

                if (
                    isinstance(art, str)
                    and getattr(handler, "_is_deep_research", False)
                    and art.lower().endswith(".docx")
                ):
                    continue

                art_id = art_obj.get("id") or str(hash(json.dumps(art_obj, ensure_ascii=False)))
                if art_id in handler.sent_artifacts:
                    continue

                handler.sent_artifacts.add(art_id)
                safe_art_obj = dict(art_obj)
                safe_art_obj.pop("path", None)
                safe_art_obj.pop("report_url", None)

                if safe_art_obj.get("type") == "file" and isinstance(art, str):
                    registered = await build_registered_artifact_metadata(
                        internal_thread_id=effective_thread_id,
                        artifact_path=art,
                        title=str(safe_art_obj.get("title") or "新工件"),
                    )
                    safe_art_obj.update(registered)

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
        except asyncio.TimeoutError:
            logger.warning("[%s] 读取图状态超时，已跳过本次工件轮询", self._label)
        except Exception:
            logger.debug("[%s] 读取图状态用于工件轮询失败", self._label, exc_info=True)

    async def _finalize_deep_research_report(
        self,
        *,
        question: str,
        effective_thread_id: str,
        handler: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        from backend.application.artifacts.artifact_utils import (
            build_registered_artifact_metadata,
        )
        from backend.application.agent_executors.image_url_registry import (
            resolve_image_tokens,
        )
        from backend.application.agent_executors.stream_utils import make_process_event
        from backend.application.tools.providers.deep_research_report_tool import (
            save_deep_research_report_direct,
        )

        yield {
            "type": "status",
            "content": "正在执行最终归档并保存研究报告...",
        }

        report_title = "深度研究报告"
        answer_text = resolve_image_tokens("".join(handler.answer_fragments).strip())
        report_content = handler.build_live_markdown_content(
            result_content=answer_text if answer_text else "本轮未形成完整自然语言结论，已先归档阶段性结果。",
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

        report_payload = await save_deep_research_report_direct(
            thread_id=effective_thread_id,
            title=report_title,
            content=report_content,
        )
        artifact_path = str(report_payload.get("artifact_path") or "")
        if not artifact_path:
            return

        handler.report_saved = True
        artifact_title = str(report_payload.get("title") or "深度研究报告")
        artifact_metadata = await build_registered_artifact_metadata(
            internal_thread_id=effective_thread_id,
            artifact_path=artifact_path,
            title=artifact_title,
            minio_object=str(report_payload.get("minio_object") or "") or None,
        )
        if not answer_text:
            fallback_answer = "主流程未返回完整结论，系统已根据已收集证据完成研究报告归档。"
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
                logger.warning("[%s] 深度研究兜底答案写入历史失败", self._label, exc_info=True)
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

    async def _resolve_history_image_tokens(self, effective_config: RunnableConfig) -> None:
        import re as _re

        from backend.application.agent_executors.image_url_registry import get_registry
        from backend.application.agent_executors.stream_utils import stringify_message_content
        from langchain_core.messages import AIMessage as _AIMessage, ToolMessage as _ToolMessage

        image_token_re = _re.compile(r"\[IMAGE:\d+\]")
        registry = get_registry()

        try:
            state = await asyncio.wait_for(
                self._graph.aget_state(effective_config),
                timeout=2.0,
            )
        except Exception as exc:
            logger.debug("[%s] 读取图状态用于 image token 解析失败: %s", self._label, exc)
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
                resolved = registry.resolve_all(raw) if not registry.is_empty() else raw
                resolved = image_token_re.sub("", resolved)
                if resolved != raw:
                    messages_to_update.append(msg.model_copy(update={"content": resolved}))
            elif isinstance(msg, _ToolMessage):
                raw = stringify_message_content(msg.content)
                if "[IMAGE:" not in raw:
                    continue
                cleaned = image_token_re.sub("", raw)
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
            logger.warning("[%s] 更新历史图片 URL 失败（不影响本次回答）: %s", self._label, exc)
