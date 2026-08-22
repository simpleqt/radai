"""Streaming event handler for platform agent executors."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, AsyncIterator

from backend.application.agent_executors.image_url_registry import (
    StreamingTokenResolver,
    inject_missing_captioned_images,
)
from backend.application.agent_executors.stream_utils import make_process_event
from backend.application.event_stream.thinking_filter import split_thinking_segments
from backend.application.image_pipeline import sanitize_answer_image_urls
from backend.config.loggers import get_logger
from backend.core.agents.context.structured_result import (
    compact_structured_result,
    structured_result_fingerprint,
)

from backend.application.agent_executors.stream_tool_evidence import (  # noqa: F401  (含测试兼容 re-export)
    _coordinated_tool_evidence_records,
    _counts_toward_project_query_budget,
    _extract_graph_message_stream,
)
from backend.application.agent_executors.stream_model_events import (
    _on_model_stream as _impl_on_model_stream,
    _on_model_end as _impl_on_model_end,
    log_session_diagnostic as _impl_log_session_diagnostic,
)
from backend.application.agent_executors.stream_tool_events import (
    _on_tool_start as _impl_on_tool_start,
    _on_tool_end as _impl_on_tool_end,
    _on_chain_end as _impl_on_chain_end,
    _on_task_runtime as _impl_on_task_runtime,
)

logger = get_logger(__name__)


class NEAgentStreamHandler:
    """Translate LangGraph events into frontend SSE payloads."""

    def __init__(
        self,
        *,
        is_deep_research: bool = False,
        thread_id: str = "",
        question: str = "",
        max_output_chars: int = 0,
        selected_skills: set[str] | None = None,
        kb_ids: list[str] | None = None,
        use_search: bool = True,
        capture_tool_evidence: bool = False,
    ):
        self._is_deep_research = is_deep_research
        self._thread_id = thread_id
        self._question = question.strip()
        self._max_output_chars = max_output_chars
        self._selected_skills: set[str] = set(selected_skills or ())
        self._kb_ids: list[str] = list(kb_ids or [])
        self._use_search = use_search
        self._capture_tool_evidence = bool(capture_tool_evidence)

        self.answer_fragments: list[str] = []
        self.collected_research_notes: list[str] = []
        self.sent_artifacts: set[str] = set()
        self.report_saved = False
        self.chunk_count = 0
        self.event_count = 0
        self.timeout_reached = False
        self.output_truncated = False
        self.deep_research_grace_applied = False
        self.deep_research_last_task_runtime_at = time.monotonic()
        self.deep_research_active_task_ids: set[str] = set()

        self._tool_started_at: dict[str, float] = {}
        self._tool_inputs: dict[str, dict[str, Any]] = {}
        self._current_gen_has_tool_calls = False
        self._normal_output_chars = 0
        self._analysis_status_sent = False
        self._generation_status_sent = False
        self._output_started_status_sent = True
        self._any_tool_executed = False
        self._live_markdown_artifact_id = (
            f"{thread_id}:deep-research-live-markdown" if thread_id else "deep-research-live-markdown"
        )
        # 每轮对话创建独立的流式 token 解析器（每次模型生成开始时重置）
        self._token_resolver = StreamingTokenResolver()
        self._html_ppt_succeeded = False
        self._skipped_stream_chunks_tool_round = 0
        self._model_generation_round = 0
        self._round_forwarded_chars = 0
        self._round_streamed_visible_text = ""
        self._round_fragment_base = 0
        self._round_reasoning_chars = 0
        self._total_skipped_tool_chunks = 0
        self._total_reasoning_chars = 0
        self._tools_invoked: list[str] = []
        self._model_round_summaries: list[dict[str, Any]] = []
        self._model_round_open = False
        self._stream_exit_reason: str | None = None
        self._malformed_tool_call_rounds = 0
        self._in_thinking_markup = False
        self._thinking_markup_carry = ""
        self._project_evidence_fragments: list[str] = []
        self._project_evidence_fingerprints: set[str] = set()
        self._retrieved_evidence_fragments: list[str] = []
        self._evidence_convergence_ready = False
        self._knowledge_target_missed = False
        self._web_evidence_fragments: list[str] = []
        self._last_evaluated_evidence_fingerprint = ""
        self._evidence_gap_text = ""
        self._project_table_query_count = 0
        self._seen_context_window_ids: set[str] = set()
        self._saw_chat_model_callbacks = False
        self._finalization_failed = False
        # writer 阶段中途失败(已流出部分正文但异常收场)置位:finalize 据此
        # 发 warning/error 级收尾事件,而不是与"撰写失败"状态矛盾的 success。
        self._writer_stage_failed = False
        # writer 兜底阶段成功把答案 _persist_answer 落库后置位:递归上限/超时
        # 路径的 should_write_fallback_state 据此跳过再次 aupdate_state,避免
        # 同一答案被追加成第二条 AIMessage(add_messages 按新 id 追加不去重)。
        self._answer_persisted_to_state = False

    def set_stream_exit_reason(self, reason: str) -> None:
        self._stream_exit_reason = reason

    @property
    def any_tool_executed(self) -> bool:
        return self._any_tool_executed

    @property
    def tools_invoked(self) -> list[str]:
        return list(self._tools_invoked)

    @property
    def model_generation_round(self) -> int:
        return self._model_generation_round

    @property
    def model_round_summaries(self) -> list[dict[str, Any]]:
        return list(self._model_round_summaries)

    @property
    def normal_output_chars(self) -> int:
        return self._normal_output_chars

    @property
    def html_ppt_succeeded(self) -> bool:
        return self._html_ppt_succeeded

    @property
    def model_round_open(self) -> bool:
        return self._model_round_open

    def mark_model_round_closed(self) -> None:
        """Close the open model round after an abnormal writer failure."""

        self._model_round_open = False

    @property
    def answer_persisted_to_state(self) -> bool:
        return self._answer_persisted_to_state

    def mark_answer_persisted_to_state(self) -> None:
        """Record that a final answer was already appended to graph state."""

        self._answer_persisted_to_state = True

    def mark_writer_stage_failed(self) -> None:
        """Record that the writer stage errored after emitting partial text."""

        self._writer_stage_failed = True

    def has_incomplete_model_round(self) -> bool:
        if self._model_round_open:
            return True
        return len(self._model_round_summaries) < self._model_generation_round

    @staticmethod
    def _stringify_evidence(value: Any, *, max_chars: int) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            text = value.strip()
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, indent=2)
            except Exception:
                logger.debug("证据 JSON 序列化失败，回退为字符串", exc_info=True)
                text = str(value)
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n...[truncated]"

    @staticmethod
    def _append_evidence(target: list[str], text: str, *, max_items: int) -> None:
        if not text.strip():
            return
        target.append(text.strip())
        if len(target) > max_items:
            del target[: len(target) - max_items]

    def _append_project_evidence(
        self,
        *,
        tool_name: str,
        raw_output: Any,
        tool_input: dict[str, Any],
    ) -> None:
        compact = compact_structured_result(raw_output, max_chars=5000)
        evidence = compact or self._stringify_evidence(raw_output, max_chars=6000)
        fingerprint = structured_result_fingerprint(raw_output)
        if fingerprint is None:
            fingerprint = hashlib.sha256(
                f"{tool_name}\0{evidence}".encode("utf-8")
            ).hexdigest()
        if fingerprint in self._project_evidence_fingerprints:
            return
        self._project_evidence_fingerprints.add(fingerprint)
        input_text = self._stringify_evidence(tool_input, max_chars=1600)
        wrapped = (
            f'<project_tool_result name="{tool_name}">\n'
            f"<tool_input>\n{input_text}\n</tool_input>\n"
            f"<tool_output>\n{evidence}\n</tool_output>\n"
            "</project_tool_result>"
        )
        self._append_evidence(
            self._project_evidence_fragments,
            wrapped,
            # ContextLedger performs the model-aware token fit. Preserve
            # distinct source blocks until then instead of applying a second
            # four-item recency window here.
            max_items=16,
        )

    @property
    def project_evidence_text(self) -> str:
        return "\n\n".join(self._project_evidence_fragments)

    @property
    def retrieved_evidence_text(self) -> str:
        return "\n\n".join([*self._retrieved_evidence_fragments, *self._web_evidence_fragments])

    def evidence_fingerprint(self) -> str:
        payload = f"{self.project_evidence_text}\n---\n{self.retrieved_evidence_text}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def should_evaluate_evidence(self, *, min_retrieval_blocks: int = 1) -> bool:
        fingerprint = self.evidence_fingerprint()
        project_only_ready = self._project_table_query_count >= 2
        retrieval_ready = (
            len(self._retrieved_evidence_fragments) >= max(1, min_retrieval_blocks)
        )
        return (
            not self._is_deep_research
            and (
                self._evidence_convergence_ready
                or retrieval_ready
                or project_only_ready
            )
            and bool(self._project_evidence_fragments)
            and (
                not self._knowledge_target_missed
                or not self._use_search
                or bool(self._web_evidence_fragments)
            )
            and fingerprint != self._last_evaluated_evidence_fingerprint
        )

    def mark_evidence_evaluated(self, fingerprint: str) -> None:
        self._last_evaluated_evidence_fingerprint = fingerprint

    @property
    def evidence_gap_text(self) -> str:
        return self._evidence_gap_text

    def record_evidence_gap(
        self,
        missing_evidence: list[str],
        next_action: str,
    ) -> None:
        """Keep the latest evaluator verdict for the no-tool writer handoff."""
        lines = [
            f"- {str(item).strip()}"
            for item in missing_evidence[:6]
            if str(item).strip()
        ]
        if str(next_action or "").strip():
            lines.append(f"- 建议补充：{str(next_action).strip()}")
        self._evidence_gap_text = "\n".join(lines)

    def should_force_evidence_convergence(self, *, max_queries: int = 8) -> bool:
        """Converge after repeated project probing has produced usable evidence."""
        return (
            not self._is_deep_research
            and self._project_table_query_count >= max(1, max_queries)
            and bool(self._project_evidence_fragments)
        )

    @staticmethod
    def _compute_missing_tail(existing_text: str, final_text: str) -> str:
        """Return only the non-overlapping tail from final_text."""
        if not final_text:
            return ""
        if not existing_text:
            return final_text
        if final_text == existing_text:
            return ""
        if final_text.startswith(existing_text):
            return final_text[len(existing_text):]
        if final_text.strip() == existing_text.strip():
            return ""
        if final_text in existing_text:
            return ""

        # 重叠尾窗封顶:含图回答里 existing(图片 token 已转 URL)与 final
        # (仍是 [IMAGE:xxx] 原文)必然子串不命中,无上限的逐字符 overlap 扫描
        # 每轮还新建切片,数万字符的长答会在事件循环上烧到秒级。真实的"未流出
        # 尾巴"远小于该窗;超出即视为整体不重叠,回退返回 final_text。
        _MAX_OVERLAP_SCAN = 4096
        max_overlap = min(len(existing_text), len(final_text), _MAX_OVERLAP_SCAN)
        existing_tail = existing_text[-max_overlap:] if max_overlap else ""
        for overlap in range(max_overlap, 0, -1):
            if existing_tail.endswith(final_text[:overlap]):
                return final_text[overlap:]

        return final_text

    def _split_thinking_markup(self, text: str) -> tuple[str, str]:
        visible, thinking, in_thinking, carry = split_thinking_segments(
            text,
            self._in_thinking_markup,
            self._thinking_markup_carry,
        )
        self._in_thinking_markup = in_thinking
        self._thinking_markup_carry = carry
        if thinking:
            self._round_reasoning_chars += len(thinking)
            self._total_reasoning_chars += len(thinking)
        return visible, thinking

    def build_live_markdown_content(
        self,
        *,
        result_content: str,
        title: str = "深度研究最终结果",
    ) -> str:
        normalized_result = result_content.strip()
        if not normalized_result:
            normalized_result = "最终结果仍在生成中，请稍候..."

        question_block = f"## 用户问题\n{self._question}\n\n" if self._question else ""
        return (
            f"# {title}\n\n"
            f"{question_block}"
            f"## 最终结果\n"
            f"{normalized_result}\n"
        )

    def build_live_markdown_artifact(
        self,
        *,
        content: str,
        title: str = "深度研究报告（实时预览）",
        is_streaming: bool = True,
    ) -> dict[str, Any]:
        detail = "深度研究 Markdown 正在实时生成" if is_streaming else "深度研究 Markdown 预览已完成"
        return {
            "id": self._live_markdown_artifact_id,
            "file_id": self._live_markdown_artifact_id,
            "title": title,
            "type": "markdown",
            "content": content,
            "previewable": True,
            "metadata": {
                "source": "deep_research_stream",
                "detail": detail,
                "question": self._question,
                "is_streaming": is_streaming,
                "auto_open": True,
                "virtual_artifact": True,
            },
        }

    async def handle(self, event: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        self.event_count += 1
        kind = event.get("event")
        tags = event.get("tags") or []
        metadata = event.get("metadata") or {}

        # Agentic RAG invokes a planner/evaluator model from inside the
        # ``multi_kb_search`` tool.  LangGraph propagates those nested model
        # callbacks to the outer event stream.  Their strict JSON responses
        # are retrieval control-plane data and must never become visible
        # ``message.delta`` answer text or alter main-model round accounting.
        internal_purpose = str(metadata.get("purpose") or "")
        is_agentic_rag_internal = (
            "agentic-rag-internal" in tags
            or metadata.get("agentic_rag_internal") is True
            or internal_purpose.startswith("agentic_rag_")
        )
        # 工具内部的嵌套模型调用（如 course_eval 的映射建议/大纲抽取 JSON 子调用）
        # 统一打 tool-internal-llm 标签：它们的流式输出是控制面数据，
        # 绝不能变成主模型的可见正文或干扰轮次记账。
        is_tool_internal_llm = (
            "tool-internal-llm" in tags
            or internal_purpose.startswith("course_eval_internal")
        )
        if is_agentic_rag_internal or is_tool_internal_llm:
            return

        if kind in {
            "on_chat_model_start",
            "on_chat_model_stream",
            "on_chat_model_end",
        }:
            self._saw_chat_model_callbacks = True

        is_large_result_summarization = (
            "large-result-subagent" in tags
            or metadata.get("purpose") == "large_result_summarization"
        )
        if is_large_result_summarization:
            if kind == "on_chat_model_start":
                yield {
                    "type": "status",
                    "content": "正在压缩较长的工具结果，请稍候；关键来源信息会继续保留...",
                }
                yield {
                    "type": "event",
                    "event": make_process_event(
                        kind="tool_start",
                        title="正在压缩工具结果",
                        detail=(
                            "工具结果正文较长，正在隔离上下文中提取结论和关键证据；"
                            "来源标识会继续保留。"
                        ),
                        tool="large_result_summarization",
                        level="active",
                    ),
                }
            elif kind == "on_chat_model_end":
                yield {"type": "status", "content": "工具结果压缩完成，继续执行任务"}
                yield {
                    "type": "event",
                    "event": make_process_event(
                        kind="tool_end",
                        title="工具结果压缩完成",
                        detail=(
                            "已将较长的工具结果压缩为主智能体可直接使用的证据摘要，"
                            "关键来源标识保持可用。"
                        ),
                        tool="large_result_summarization",
                        level="success",
                    ),
                }
            return

        is_summarization = (
            "summarization" in tags
            or "history-compaction" in tags
            or metadata.get("purpose") in {"summarization", "history_compaction"}
        )

        if is_summarization:
            if kind == "on_chat_model_start":
                yield {"type": "status", "content": "正在压缩历史上下文..."}
                yield {
                    "type": "event",
                    "event": make_process_event(
                        kind="tool_start",
                        title="压缩历史上下文",
                        detail="正在精简历史会话以节省上下文空间...",
                        tool="context_compression",
                        level="active",
                    ),
                }
            elif kind == "on_chat_model_end":
                yield {"type": "status", "content": "历史上下文压缩完成"}
                yield {
                    "type": "event",
                    "event": make_process_event(
                        kind="tool_end",
                        title="历史上下文压缩完成",
                        detail="已成功精简历史会话，节省 Token 消耗",
                        tool="context_compression",
                        level="success",
                    ),
                }
            return

        if kind == "on_chat_model_stream":
            async for item in self._on_model_stream(event):
                yield item
        elif kind == "on_chat_model_start":
            self._start_model_round()
        elif kind == "on_chat_model_end":
            async for item in self._on_model_end(event):
                yield item
        elif kind == "on_tool_start":
            async for item in self._on_tool_start(event):
                yield item
        elif kind == "on_tool_end":
            async for item in self._on_tool_end(event):
                yield item
        elif kind == "on_chain_start":
            async for item in self._on_chain_start(event):
                yield item
        elif kind == "on_chain_end":
            async for item in self._on_chain_end(event):
                yield item
        elif kind == "on_chain_stream":
            graph_message = _extract_graph_message_stream(event)
            if graph_message is not None and not self._saw_chat_model_callbacks:
                async for item in self._on_graph_message_stream(graph_message):
                    yield item
                return
            async for item in self._on_task_runtime(event):
                yield item
        elif kind == "on_custom_event":
            async for item in self._on_task_runtime(event):
                yield item

    def _start_model_round(self) -> None:
        self._model_generation_round += 1
        self._skipped_stream_chunks_tool_round = 0
        self._current_gen_has_tool_calls = False
        self._round_forwarded_chars = 0
        self._round_streamed_visible_text = ""
        # 本轮片段基线：工具轮幽灵正文对账时把 answer_fragments[base:] 整体
        # 替换成留存文本（见 stream_model_events._on_model_end）
        self._round_fragment_base = len(self.answer_fragments)
        self._round_reasoning_chars = 0
        self._model_round_open = True
        self._token_resolver = StreamingTokenResolver()
        if not self._generation_status_sent:
            self._generation_status_sent = True
        logger.info(
            "[NEAgentStreamHandler] 模型轮次开始: round=%d thread_id=%s deep_research=%s",
            self._model_generation_round,
            self._thread_id,
            self._is_deep_research,
        )

    async def _on_graph_message_stream(
        self,
        message: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Fallback for LangGraph versions that expose model output as chain data."""

        if not self._model_round_open:
            self._start_model_round()
        if getattr(message, "tool_calls", None):
            self._current_gen_has_tool_calls = True
        async for item in self._on_model_stream(
            {"event": "on_chat_model_stream", "data": {"chunk": message}}
        ):
            yield item

        is_chunk = type(message).__name__.endswith("Chunk")
        is_last_chunk = str(getattr(message, "chunk_position", "") or "") == "last"
        if not is_chunk or is_last_chunk:
            async for item in self._on_model_end(
                {"event": "on_chat_model_end", "data": {"output": message}}
            ):
                yield item

    # 实现在 stream_model_events.py(模块级函数,self 经 descriptor 绑定)
    log_session_diagnostic = _impl_log_session_diagnostic

    async def finalize(self) -> AsyncIterator[dict[str, Any]]:
        self.log_session_diagnostic()

        if (
            not self._is_deep_research
            and "html-ppt" in self._selected_skills
            and not "".join(self.answer_fragments).strip()
            and not self._html_ppt_succeeded
        ):
            logger.info(
                "[NEAgentStreamHandler] finalize 注入 html-ppt 引导: thread_id=%s, "
                "event_count=%d, tools_executed=%s, visible_chars=%d",
                self._thread_id,
                self.event_count,
                self._any_tool_executed,
                self._normal_output_chars,
            )
            msg = (
                "本轮没有输出任何正文，也未成功生成 HTML 幻灯片。\n\n"
                "请按顺序重试：先 `read_local_skill_file(\"html-ppt\")`，"
                "再视需要 `multi_kb_search`，最后调用 `generate_html_ppt` 交付工件。\n"
                "若你只需问答、不要幻灯片，请在前端关闭 html-ppt skill 或明确说明仅需文字回答。\n"
            )
            self.answer_fragments.append(msg)
            self.chunk_count += 1
            self._normal_output_chars += len(msg)
            yield {"type": "chunk", "content": msg}

        if (
            not self._is_deep_research
            and self.has_incomplete_model_round()
        ):
            # 通用平台文案:不得写死某个业务域(如地下水)的操作指引,
            # 领域特定引导应由领域 prompt/skill 提供。
            msg = (
                "\n\n本轮已执行检索和工具调用，但模型最终答案没有正常收尾，"
                "系统已停止继续等待。请重新发起本问题；必要时请明确需要分析的"
                "对象、指标或范围，系统会重新查询并整理答案。"
            )
            self.answer_fragments.append(msg)
            self.chunk_count += 1
            self._normal_output_chars += len(msg)
            yield {"type": "chunk", "content": msg}

        if (
            not self._is_deep_research
            and self.timeout_reached
            and not "".join(self.answer_fragments).strip()
        ):
            msg = (
                "当前流程在生成最终答案前超时，已停止继续检索。"
                "本次问题和已完成的查询会保留在历史记录中；请重新发送问题以继续。"
            )
            self.answer_fragments.append(msg)
            self.chunk_count += 1
            self._normal_output_chars += len(msg)
            yield {"type": "chunk", "content": msg}

        if (
            not self._is_deep_research
            and self._malformed_tool_call_rounds > 0
            and not "".join(self.answer_fragments).strip()
        ):
            msg = (
                "模型返回了工具调用结束信号，但没有返回可执行的工具名称或参数，本轮已停止。\n\n"
                "这通常是 OpenAI 兼容网关的流式 tool-calling 响应不完整导致的。"
                "系统已切换为工具调用轮次非流式模式，请重新发起本轮请求。"
            )
            self.answer_fragments.append(msg)
            self.chunk_count += 1
            self._normal_output_chars += len(msg)
            yield {"type": "chunk", "content": msg}

        if not self._is_deep_research and not "".join(self.answer_fragments).strip():
            self._finalization_failed = True
            msg = (
                "本轮流程已经结束，但模型没有生成可展示的最终答案。"
                "已完成的检索和工具调用记录仍保留在会话中，请重新发送问题后重试。"
            )
            self.answer_fragments.append(msg)
            self.chunk_count += 1
            self._normal_output_chars += len(msg)
            yield {"type": "chunk", "content": msg}

        current_answer = "".join(self.answer_fragments)
        corrected_answer = inject_missing_captioned_images(current_answer)
        corrected_answer = sanitize_answer_image_urls(corrected_answer)
        if corrected_answer != current_answer:
            self.answer_fragments = [corrected_answer]
            self._normal_output_chars = len(corrected_answer)
            yield {
                "type": "message.replace",
                "payload": {"content": corrected_answer},
            }

        if self._finalization_failed:
            detail = (
                f"回答生成失败：共处理 {self.event_count} 个事件，"
                f"生成 {self.chunk_count} 个错误提示片段"
            )
        elif self._writer_stage_failed:
            detail = (
                f"撰写阶段中途失败，已交付部分正文：共处理 {self.event_count} 个事件，"
                f"生成 {self.chunk_count} 个输出片段"
            )
        else:
            detail = (
                f"本轮共处理 {self.event_count} 个事件，生成 {self.chunk_count} 个输出片段"
                if not self.timeout_reached
                else f"流程已提前收敛：共处理 {self.event_count} 个事件，生成 {self.chunk_count} 个输出片段"
            )
        answer_failed = self._finalization_failed
        answer_degraded = self._writer_stage_failed and not answer_failed
        yield {
            "type": "event",
            "event": make_process_event(
                kind="answer",
                title=(
                    "回答生成失败"
                    if answer_failed
                    else ("回答未完整收尾" if answer_degraded else "回答完成")
                ),
                detail=detail,
                level=(
                    "error"
                    if answer_failed
                    else ("warning" if answer_degraded else "success")
                ),
                metadata={
                    "event_count": self.event_count,
                    "chunk_count": self.chunk_count,
                    "timeout_reached": self.timeout_reached,
                    "output_truncated": self.output_truncated,
                    "finalization_failed": self._finalization_failed,
                },
            ),
        }

    # 实现在 stream_model_events.py(模块级函数,self 经 descriptor 绑定)
    _on_model_stream = _impl_on_model_stream

    # 实现在 stream_model_events.py(模块级函数,self 经 descriptor 绑定)
    _on_model_end = _impl_on_model_end

    @staticmethod
    def _tool_run_id(event: dict[str, Any]) -> str:
        metadata = event.get("metadata") or {}
        data = event.get("data") or {}
        candidates = (
            event.get("run_id"),
            metadata.get("tool_run_id") if isinstance(metadata, dict) else None,
            metadata.get("tool_call_id") if isinstance(metadata, dict) else None,
            data.get("tool_call_id") if isinstance(data, dict) else None,
        )
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value:
                return value
        return ""

    # 实现在 stream_tool_events.py(模块级函数,self 经 descriptor 绑定)
    _on_tool_start = _impl_on_tool_start

    # 实现在 stream_tool_events.py(模块级函数,self 经 descriptor 绑定)
    _on_tool_end = _impl_on_tool_end

    async def _on_chain_start(self, event: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        chain_name = str(event.get("name", ""))
        if not self._analysis_status_sent and "ne-full-feature" in chain_name:
            self._analysis_status_sent = True
            yield {"type": "status", "content": "正在理解问题并规划回答..."}
        if "task" in chain_name.lower():
            task_name = event.get("name", "task")
            yield {"type": "status", "content": "正在委派任务给子智能体..."}
            yield {
                "type": "event",
                "event": make_process_event(
                    kind="task_start",
                    title="任务委派",
                    detail=f"正在委派任务: {task_name}",
                    tool=task_name,
                    level="active",
                ),
            }

    # 实现在 stream_tool_events.py(模块级函数,self 经 descriptor 绑定)
    _on_chain_end = _impl_on_chain_end

    # 实现在 stream_tool_events.py(模块级函数,self 经 descriptor 绑定)
    _on_task_runtime = _impl_on_task_runtime
