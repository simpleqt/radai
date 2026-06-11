"""Shared LangGraph streaming runtime for profiled agents."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError

from backend.application.agent_executors.writer_stage import EvidenceWriterStage, WriterStageConfig
from backend.core.skills.skill_runtime_state import (
    get_missing_required_skills,
    reset_request_skill_state,
    set_request_skill_state,
)
from backend.core.tools.request_limits import (
    reset_tool_budgets,
    reset_turn_kb_context_budget,
    set_tool_budgets,
)

logger = logging.getLogger(__name__)

EventAsyncIterator = AsyncIterator[dict[str, Any]]
ArtifactEmitter = Callable[..., EventAsyncIterator]
DeepResearchFinalizer = Callable[..., EventAsyncIterator]
HistoryImageResolver = Callable[[RunnableConfig], Any]


async def wait_for_stream_event(
    event_iter: AsyncIterator[dict[str, Any]],
    pending_task: asyncio.Task[dict[str, Any]] | None,
    *,
    timeout: float,
) -> tuple[dict[str, Any] | None, asyncio.Task[dict[str, Any]] | None]:
    """Wait for one event without cancelling the upstream iterator on heartbeat."""
    task = pending_task or asyncio.create_task(event_iter.__anext__())
    try:
        event = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except asyncio.TimeoutError:
        return None, task
    return event, None


async def cancel_pending_stream_event(
    task: asyncio.Task[dict[str, Any]] | None,
) -> None:
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, StopAsyncIteration):
        pass
    except Exception:
        logger.debug("关闭待处理事件读取任务时忽略异常", exc_info=True)


@dataclass(frozen=True)
class StreamRuntimeConfig:
    label: str
    graph: Any
    model_name: str
    kb_ids: list[str]
    use_search: bool
    subagent_enabled: bool
    max_concurrent_subagents: int
    selected_skills: set[str] | None
    task_type: str | None
    lightweight_mode: bool
    lightweight_thread_id: str | None
    thinking_enabled: bool
    prompt_domain: str
    guardrail_policy: str
    runtime_permissions: dict[str, Any]
    guardrail_instructions: str
    time_context: str
    lightweight_allowed_tools: list[str]
    normal_max_output_chars: int
    multi_kb_search_max_calls: int
    deep_research_task_type: str
    deep_research_stream_timeout_seconds: int
    deep_research_idle_timeout_seconds: int
    deep_research_finalize_idle_timeout_seconds: int
    deep_research_task_stall_grace_seconds: int
    deep_research_finalize_grace_seconds: int
    emit_graph_artifacts: ArtifactEmitter
    finalize_deep_research_report: DeepResearchFinalizer
    resolve_history_image_tokens: HistoryImageResolver
    warn_low_visible_output: bool = False


class AgentStreamRuntime:
    """Run the common LangGraph SSE loop used by full-feature agents."""

    def __init__(self, config: StreamRuntimeConfig):
        self._config = config

    async def aquery_stream(
        self,
        *,
        question: str,
        thread_id: str | None = None,
        config: RunnableConfig | None = None,
        image_contents: list[dict] | None = None,
    ) -> EventAsyncIterator:
        from backend.application.agent_executors.stream_handler import NEAgentStreamHandler
        from backend.application.agent_executors.stream_timeouts import (
            INCOMPLETE_ROUND_STREAM_RETRY_MAX,
            MODEL_ROUND_IDLE_TIMEOUT_SECONDS,
            STREAM_HEARTBEAT_INTERVAL_SECONDS,
            STREAM_IDLE_TIMEOUT_SECONDS,
            resolve_normal_stream_idle_timeout,
        )

        cfg = self._config
        effective_config = dict(config or {})
        effective_config["configurable"] = dict(effective_config.get("configurable") or {})

        effective_thread_id = thread_id or str(uuid.uuid4())
        effective_config["configurable"]["thread_id"] = effective_thread_id
        effective_config["configurable"]["kb_ids"] = cfg.kb_ids
        effective_config["configurable"]["use_search"] = cfg.use_search
        effective_config["configurable"]["subagent_enabled"] = cfg.subagent_enabled
        effective_config["configurable"]["max_concurrent_subagents"] = cfg.max_concurrent_subagents
        if cfg.task_type:
            effective_config["configurable"]["task_type"] = cfg.task_type
        if cfg.selected_skills is not None:
            effective_config["configurable"]["selected_skills"] = sorted(cfg.selected_skills)

        effective_config["metadata"] = dict(effective_config.get("metadata") or {})
        effective_config["metadata"]["model_name"] = cfg.model_name
        effective_config["recursion_limit"] = 100

        stream_started_at = time.monotonic()
        stream_started_epoch_ms = int(time.time() * 1000)
        first_event_delay_ms: int | None = None
        first_model_event_delay_ms: int | None = None
        first_chunk_delay_ms: int | None = None
        logger.info(
            "[%s] 开始流式查询, thread_id=%s, model=%s, thinking=%s, search=%s, subagent=%s",
            cfg.label,
            effective_thread_id,
            cfg.model_name,
            cfg.thinking_enabled,
            cfg.use_search,
            cfg.subagent_enabled,
        )

        from backend.application.agent_executors.image_url_registry import reset_registry

        reset_registry()

        if cfg.lightweight_mode:
            from backend.application.agent_executors.lightweight_qa_agent import LightweightQAAgent

            lightweight_agent = LightweightQAAgent(
                graph=cfg.graph,
                model_name=cfg.model_name,
                kb_ids=list(cfg.kb_ids),
                use_search=cfg.use_search,
                node_name="model",
                thread_id=cfg.lightweight_thread_id,
                thinking_enabled=cfg.thinking_enabled,
                prompt_domain=cfg.prompt_domain,
                guardrail_policy=cfg.guardrail_policy,
                runtime_permissions=dict(cfg.runtime_permissions),
                guardrail_instructions=cfg.guardrail_instructions,
                time_context=cfg.time_context,
                allowed_tools=list(cfg.lightweight_allowed_tools),
                use_direct_query=str(cfg.lightweight_thread_id or "").startswith("explore__"),
            )
            async for event in lightweight_agent.aquery_stream(
                question=question,
                effective_config=effective_config,
                stream_started_at=stream_started_at,
                image_contents=image_contents,
            ):
                yield event
            return

        reset_registry()
        reset_turn_kb_context_budget()

        configurable = effective_config.get("configurable", {})
        runtime_task_type = str(configurable.get("task_type") or cfg.task_type or "").strip()
        deep_research_mode = runtime_task_type == cfg.deep_research_task_type
        deep_research_timeout_budget = float(cfg.deep_research_stream_timeout_seconds)
        deep_research_finalize_budget_applied = False
        last_upstream_event_at = stream_started_at
        handler = NEAgentStreamHandler(
            is_deep_research=deep_research_mode,
            thread_id=effective_thread_id,
            question=question,
            max_output_chars=cfg.normal_max_output_chars,
            selected_skills=cfg.selected_skills,
            kb_ids=cfg.kb_ids,
            use_search=cfg.use_search,
        )

        budget_token = set_tool_budgets(
            {"multi_kb_search": 3, "web_search": 10, "academic_search": 5}
            if deep_research_mode
            else {"multi_kb_search": cfg.multi_kb_search_max_calls, "web_search": 10, "academic_search": 2}
            if cfg.selected_skills and "academic-search" in cfg.selected_skills
            else (
                {"multi_kb_search": 3, "web_search": 10}
                if deep_research_mode
                else {"multi_kb_search": cfg.multi_kb_search_max_calls, "web_search": 10}
            )
        )
        skill_state_token = set_request_skill_state(cfg.selected_skills)
        event_stream = None
        event_iter = None
        pending_event_task = None
        graph_recursion_error = False

        try:
            yield {"type": "status", "content": "已接收问题，正在分析..."}
            if deep_research_mode:
                yield {
                    "type": "status",
                    "content": "深度研究模式已启用：将拆解任务、并行研究并放宽检索轮数。",
                }

            user_content = self._build_user_content(question, image_contents)
            logger.info(
                "[%s] 流式等待配置: idle=%ds model_round_idle=%ds heartbeat=%ds retry=%d",
                cfg.label,
                STREAM_IDLE_TIMEOUT_SECONDS,
                MODEL_ROUND_IDLE_TIMEOUT_SECONDS,
                STREAM_HEARTBEAT_INTERVAL_SECONDS,
                INCOMPLETE_ROUND_STREAM_RETRY_MAX,
            )

            stream_exit_reason = "unknown"
            max_stream_passes = 1 if deep_research_mode else (1 + INCOMPLETE_ROUND_STREAM_RETRY_MAX)
            stream_pass = 0

            while stream_pass < max_stream_passes:
                if stream_pass == 0:
                    graph_input: dict | None = {"messages": [{"role": "user", "content": user_content}]}
                else:
                    if stream_exit_reason != "langgraph_stream_completed" or not handler.has_incomplete_model_round():
                        break
                    logger.warning(
                        "[%s] 模型轮次未完成，从 checkpoint 续跑 pass=%d/%d thread_id=%s",
                        cfg.label,
                        stream_pass,
                        INCOMPLETE_ROUND_STREAM_RETRY_MAX,
                        effective_thread_id,
                    )
                    yield {"type": "status", "content": "模型仍在生成中，正在延长等待并尝试续跑..."}
                    graph_input = None
                    last_upstream_event_at = time.monotonic()

                event_stream = cfg.graph.astream_events(
                    graph_input,
                    config=effective_config,
                    version="v2",
                    stream_mode=["messages", "custom", "updates", "tasks"],
                )
                event_iter = event_stream.__aiter__()
                pending_event_task = None
                stream_exit_reason = "unknown"

                while True:
                    should_break_stream = False
                    try:
                        if deep_research_mode:
                            if handler.deep_research_grace_applied and not handler.deep_research_active_task_ids:
                                idle_timeout = cfg.deep_research_finalize_idle_timeout_seconds
                            else:
                                idle_timeout = cfg.deep_research_idle_timeout_seconds
                        else:
                            idle_timeout = resolve_normal_stream_idle_timeout(
                                handler=handler,
                                deep_research_mode=False,
                            )

                        if deep_research_mode:
                            elapsed_seconds = time.monotonic() - stream_started_at
                            remaining_total = deep_research_timeout_budget - elapsed_seconds
                            if remaining_total <= 0:
                                handler.timeout_reached = True
                                stream_exit_reason = "deep_research_total_timeout"
                                yield {
                                    "type": "status",
                                    "content": "深度研究执行超时，正在整理阶段性结果并强制生成研究报告...",
                                }
                                break
                            wait_timeout = min(idle_timeout, max(1.0, remaining_total))
                        else:
                            idle_elapsed = max(0.0, time.monotonic() - last_upstream_event_at)
                            remaining_idle = max(1.0, idle_timeout - idle_elapsed)
                            wait_timeout = min(STREAM_HEARTBEAT_INTERVAL_SECONDS, remaining_idle)

                        event, pending_event_task = await wait_for_stream_event(
                            event_iter,
                            pending_event_task,
                            timeout=wait_timeout,
                        )
                        if event is None:
                            raise asyncio.TimeoutError
                    except StopAsyncIteration:
                        pending_event_task = None
                        stream_exit_reason = "langgraph_stream_completed"
                        break
                    except GraphRecursionError:
                        pending_event_task = None
                        graph_recursion_error = True
                        stream_exit_reason = "graph_recursion_limit"
                        handler.timeout_reached = True
                        logger.error(
                            "[%s] LangGraph 达到递归上限，停止继续调用工具: thread_id=%s tools=%s",
                            cfg.label,
                            effective_thread_id,
                            handler._tools_invoked,
                        )
                        yield {
                            "type": "status",
                            "content": "工具调用次数过多，已停止继续检索并基于现有证据生成答案...",
                        }
                        break
                    except asyncio.TimeoutError:
                        if not deep_research_mode:
                            idle_elapsed = time.monotonic() - last_upstream_event_at
                            idle_timeout = resolve_normal_stream_idle_timeout(
                                handler=handler,
                                deep_research_mode=False,
                            )
                            if idle_elapsed < idle_timeout:
                                yield {"type": "ping", "timestamp": int(time.time() * 1000)}
                                continue

                        if deep_research_mode:
                            now_monotonic = time.monotonic()
                            elapsed_seconds = now_monotonic - stream_started_at
                            if elapsed_seconds < deep_research_timeout_budget:
                                yield {
                                    "type": "status",
                                    "content": "深度研究仍在执行，正在汇总结果并准备保存报告...",
                                }
                                continue

                            task_stall_seconds = now_monotonic - handler.deep_research_last_task_runtime_at
                            if (
                                handler.deep_research_active_task_ids
                                and task_stall_seconds < cfg.deep_research_task_stall_grace_seconds
                            ):
                                yield {"type": "status", "content": "子任务仍在运行，继续等待并汇总最新研究结果..."}
                                continue

                        handler.timeout_reached = True
                        stream_exit_reason = "deep_research_idle_timeout" if deep_research_mode else "idle_timeout"
                        yield {
                            "type": "status",
                            "content": (
                                "深度研究阶段长时间无新进展，正在强制收敛并生成研究报告..."
                                if deep_research_mode
                                else "当前流程长时间无新进展，正在提前收敛并返回阶段性结果..."
                            ),
                        }
                        break

                    last_upstream_event_at = time.monotonic()
                    kind = event.get("event")
                    name = event.get("name", "")

                    if first_event_delay_ms is None:
                        first_event_delay_ms = int((time.monotonic() - stream_started_at) * 1000)
                        logger.info("[%s] 首个 LangGraph 事件耗时: %dms, kind=%s, name=%s", cfg.label, first_event_delay_ms, kind, name)
                    if kind == "on_chat_model_start" and first_model_event_delay_ms is None:
                        first_model_event_delay_ms = int((time.monotonic() - stream_started_at) * 1000)
                        logger.info("[%s] 首个模型事件耗时: %dms", cfg.label, first_model_event_delay_ms)
                    if handler.event_count <= 10 or kind == "on_chat_model_stream":
                        logger.debug("[%s] 事件 #%s: kind=%s, name=%s", cfg.label, handler.event_count + 1, kind, name)

                    async for sse in handler.handle(event):
                        if sse.get("type") == "chunk" and first_chunk_delay_ms is None:
                            first_chunk_delay_ms = int((time.monotonic() - stream_started_at) * 1000)
                            logger.info("[%s] 首个输出 chunk 耗时: %dms", cfg.label, first_chunk_delay_ms)
                        yield sse
                        if not deep_research_mode and handler.output_truncated:
                            should_break_stream = True

                    if deep_research_mode and handler.deep_research_grace_applied and not deep_research_finalize_budget_applied:
                        deep_research_timeout_budget += cfg.deep_research_finalize_grace_seconds
                        deep_research_finalize_budget_applied = True

                    if kind in {"on_tool_end", "on_chain_end", "on_custom_event", "on_chain_stream"}:
                        async for artifact_event in cfg.emit_graph_artifacts(
                            effective_config=effective_config,
                            effective_thread_id=effective_thread_id,
                            handler=handler,
                        ):
                            yield artifact_event

                    if (
                        not deep_research_mode
                        and cfg.prompt_domain == "groundwater"
                        and kind == "on_tool_end"
                        and handler.should_handoff_to_writer(min_retrieval_blocks=1)
                    ):
                        stream_exit_reason = "writer_handoff"
                        yield {
                            "type": "status",
                            "content": "检索智能体已完成取证，正在交给撰写智能体生成答案...",
                        }
                        break

                    if deep_research_mode and (time.monotonic() - stream_started_at) > deep_research_timeout_budget:
                        handler.timeout_reached = True
                        stream_exit_reason = "deep_research_budget_exceeded"
                        yield {
                            "type": "status",
                            "content": "深度研究执行超时，正在整理阶段性结果并强制生成研究报告...",
                        }
                        break

                    if should_break_stream:
                        stream_exit_reason = "output_truncated"
                        break

                await cancel_pending_stream_event(pending_event_task)
                pending_event_task = None

                stream_pass += 1
                if stream_exit_reason != "langgraph_stream_completed" or not handler.has_incomplete_model_round():
                    break

            handler.set_stream_exit_reason(stream_exit_reason)
            idle_since_last_event_ms = int((time.monotonic() - last_upstream_event_at) * 1000)
            logger.info(
                "[%s] LangGraph 事件流结束 reason=%s thread_id=%s events=%d "
                "idle_since_last_event_ms=%d open_model_round=%s",
                cfg.label,
                stream_exit_reason,
                effective_thread_id,
                handler.event_count,
                idle_since_last_event_ms,
                handler.has_incomplete_model_round(),
            )
            if handler.has_incomplete_model_round():
                logger.warning(
                    "[%s] 检测到未完成的模型轮次: thread_id=%s started_rounds=%d ended_rounds=%d stream_exit=%s",
                    cfg.label,
                    effective_thread_id,
                    handler._model_generation_round,
                    len(handler._model_round_summaries),
                    stream_exit_reason,
                )

            if (
                not deep_research_mode
                and stream_exit_reason in {"writer_handoff", "graph_recursion_limit"}
                and not "".join(handler.answer_fragments).strip()
                and bool(handler.project_evidence_text or handler.retrieved_evidence_text)
            ):
                # The graph may keep running after the retrieval handoff and start
                # another model round. Close it before launching the no-tool writer.
                try:
                    if event_iter is not None and hasattr(event_iter, "aclose"):
                        await asyncio.wait_for(event_iter.aclose(), timeout=1.5)
                    elif event_stream is not None and hasattr(event_stream, "aclose"):
                        await asyncio.wait_for(event_stream.aclose(), timeout=1.5)
                except Exception:
                    logger.debug("[%s] writer handoff 时关闭原事件流失败", cfg.label, exc_info=True)
                finally:
                    event_iter = None
                    event_stream = None

                writer_stage = EvidenceWriterStage(
                    WriterStageConfig(
                        label=cfg.label,
                        graph=cfg.graph,
                        model_name=cfg.model_name,
                    )
                )
                async for writer_event in writer_stage.stream(
                    question=question,
                    effective_config=effective_config,
                    handler=handler,
                ):
                    if writer_event.get("type") == "chunk" and first_chunk_delay_ms is None:
                        first_chunk_delay_ms = int((time.monotonic() - stream_started_at) * 1000)
                        logger.info("[%s] 首个撰写输出 chunk 耗时: %dms", cfg.label, first_chunk_delay_ms)
                    yield writer_event

            if deep_research_mode and not handler.report_saved:
                async for event in cfg.finalize_deep_research_report(
                    question=question,
                    effective_thread_id=effective_thread_id,
                    handler=handler,
                ):
                    yield event
                await cfg.resolve_history_image_tokens(effective_config)

            async for final_event in handler.finalize():
                yield final_event

            if not deep_research_mode:
                await cfg.resolve_history_image_tokens(effective_config)

            if handler.timeout_reached and not handler.answer_fragments and not graph_recursion_error:
                yield {
                    "type": "chunk",
                    "content": (
                        "深度研究主流程超时，已停止继续等待并返回当前可用阶段性结果。"
                        if deep_research_mode
                        else "当前流程超时，系统已提前结束本轮并返回可用阶段性结果；建议新建会话后重试。"
                    ),
                }

            total_ms = int((time.monotonic() - stream_started_at) * 1000)
            answer_len = len("".join(handler.answer_fragments))
            logger.info(
                "[%s] 流式查询完成, 共 %d 个事件, %d 个 chunk, answer_chars=%d "
                "visible_chars=%d timeout=%s truncated=%s, total=%dms, "
                "first_event=%sms, first_model=%sms, first_chunk=%sms",
                cfg.label,
                handler.event_count,
                handler.chunk_count,
                answer_len,
                handler._normal_output_chars,
                handler.timeout_reached,
                handler.output_truncated,
                total_ms,
                first_event_delay_ms,
                first_model_event_delay_ms,
                first_chunk_delay_ms,
            )
            if (
                cfg.warn_low_visible_output
                and not deep_research_mode
                and handler._normal_output_chars < 300
                and (handler._any_tool_executed or handler._model_generation_round > 0)
            ):
                logger.warning(
                    "[%s] 本轮可见输出偏少: thread_id=%s answer_chars=%d model_rounds=%d tools=%s html_ppt_ok=%s",
                    cfg.label,
                    effective_thread_id,
                    handler._normal_output_chars,
                    handler._model_generation_round,
                    handler._tools_invoked,
                    handler._html_ppt_succeeded,
                )
        finally:
            await cancel_pending_stream_event(pending_event_task)
            try:
                if event_iter is not None and hasattr(event_iter, "aclose"):
                    await asyncio.wait_for(event_iter.aclose(), timeout=1.5)
                elif event_stream is not None and hasattr(event_stream, "aclose"):
                    await asyncio.wait_for(event_stream.aclose(), timeout=1.5)
            except Exception:
                logger.debug("[%s] 关闭事件流时忽略异常", cfg.label, exc_info=True)
            reset_tool_budgets(budget_token)
            missing_skills = get_missing_required_skills()
            if missing_skills:
                logger.warning("[%s] 本轮结束仍有未加载 Skills: %s", cfg.label, sorted(missing_skills))
            reset_request_skill_state(skill_state_token)

    @staticmethod
    def _build_user_content(question: str, image_contents: list[dict] | None) -> list[dict] | str:
        if not image_contents:
            return question
        user_content: list[dict] = [{"type": "text", "text": question}]
        for image in image_contents:
            if image.get("image_url"):
                user_content.append({"type": "image_url", "image_url": {"url": image["image_url"]}})
                continue
            if image.get("base64"):
                b64 = image.get("base64", "")
                mime = image.get("mime_type", "image/png")
                user_content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        return user_content
