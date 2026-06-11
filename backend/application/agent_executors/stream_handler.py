"""Streaming event handler for platform agent executors."""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

from backend.application.artifacts.artifact_utils import build_registered_artifact_metadata
from backend.config.loggers import get_logger
from backend.application.agent_executors.image_url_registry import StreamingTokenResolver, resolve_image_tokens
from backend.application.agents.prompt_templates.ne import SUPPRESSED_TOOL_RESULT_TOOLS
from backend.application.agent_executors.stream_utils import (
    build_task_runtime_metadata,
    extract_chat_model_output_text,
    extract_reasoning_content,
    extract_report_metadata,
    extract_task_message_text,
    extract_task_result_metadata,
    extract_task_runtime_event_payload,
    extract_web_search_metadata,
    iter_recovered_text_chunks,
    make_process_event,
    stringify_message_content,
    summarize_tool_output,
)
from backend.application.event_stream.thinking_filter import split_thinking_segments

logger = get_logger(__name__)

# 可见正文低于该阈值且执行过工具时，会话结束打 WARNING 便于排查「几乎无输出」
_LOW_VISIBLE_OUTPUT_WARN_CHARS = 300
_GROUNDWATER_PROJECT_EVIDENCE_TOOLS = {
    "groundwater_project_skill_read",
    "query_project_tables",
    "groundwater_prediction",
    "groundwater_quality_check",
}


def _extract_ai_message_from_output(output: Any) -> Any | None:
    """Best-effort extract AIMessage from on_chat_model_end payload."""
    if output is None:
        return None
    if hasattr(output, "tool_calls") or getattr(output, "type", None) == "ai":
        return output
    if isinstance(output, dict):
        for key in ("message", "output", "result"):
            if key in output:
                msg = _extract_ai_message_from_output(output.get(key))
                if msg is not None:
                    return msg
        generations = output.get("generations")
        if generations:
            return _extract_ai_message_from_output(generations)
        return None
    if isinstance(output, list):
        for item in output:
            msg = _extract_ai_message_from_output(item)
            if msg is not None:
                return msg
        return None
    if hasattr(output, "message"):
        return _extract_ai_message_from_output(getattr(output, "message"))
    if hasattr(output, "generations"):
        return _extract_ai_message_from_output(getattr(output, "generations"))
    return None


def _extract_finish_reason(output: Any) -> str | None:
    if output is None:
        return None
    if isinstance(output, dict):
        for key in ("finish_reason", "stop_reason"):
            value = output.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("message", "output", "result", "generations"):
            if key in output:
                reason = _extract_finish_reason(output.get(key))
                if reason:
                    return reason
        return None
    if isinstance(output, list):
        for item in output:
            reason = _extract_finish_reason(item)
            if reason:
                return reason
        return None
    generation_info = getattr(output, "generation_info", None)
    if isinstance(generation_info, dict):
        for key in ("finish_reason", "stop_reason"):
            value = generation_info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    response_metadata = getattr(output, "response_metadata", None)
    if isinstance(response_metadata, dict):
        value = response_metadata.get("finish_reason") or response_metadata.get("stop_reason")
        if isinstance(value, str) and value.strip():
            return value.strip()
    if hasattr(output, "message"):
        return _extract_finish_reason(getattr(output, "message"))
    if hasattr(output, "generations"):
        return _extract_finish_reason(getattr(output, "generations"))
    return None


def _extract_tool_call_names(output: Any) -> list[str]:
    msg = _extract_ai_message_from_output(output)
    if msg is None:
        return []
    tool_calls = getattr(msg, "tool_calls", None) or []
    if not tool_calls:
        additional_kwargs = getattr(msg, "additional_kwargs", None)
        if isinstance(additional_kwargs, dict):
            tool_calls = additional_kwargs.get("tool_calls") or additional_kwargs.get("invalid_tool_calls") or []
    names: list[str] = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            function = tc.get("function")
            if isinstance(function, dict):
                name = str(function.get("name") or tc.get("name") or "").strip()
            else:
                name = str(tc.get("name") or "").strip()
        else:
            name = str(getattr(tc, "name", None) or "").strip()
        if name:
            names.append(name)
    return names


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
    ):
        self._is_deep_research = is_deep_research
        self._thread_id = thread_id
        self._question = question.strip()
        self._max_output_chars = max_output_chars
        self._selected_skills: set[str] = set(selected_skills or ())
        self._kb_ids: list[str] = list(kb_ids or [])
        self._use_search = use_search

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
        self._retrieved_evidence_fragments: list[str] = []
        self._writer_handoff_ready = False
        self._knowledge_target_missed = False
        self._web_evidence_fragments: list[str] = []

    def set_stream_exit_reason(self, reason: str) -> None:
        self._stream_exit_reason = reason

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

    @property
    def project_evidence_text(self) -> str:
        return "\n\n".join(self._project_evidence_fragments)

    @property
    def retrieved_evidence_text(self) -> str:
        return "\n\n".join([*self._retrieved_evidence_fragments, *self._web_evidence_fragments])

    def should_handoff_to_writer(self, *, min_retrieval_blocks: int = 1) -> bool:
        return (
            not self._is_deep_research
            and (
                self._writer_handoff_ready
                or len(self._retrieved_evidence_fragments) >= max(1, min_retrieval_blocks)
            )
            and bool(self._project_evidence_fragments)
            and bool(self._retrieved_evidence_fragments)
            and (
                not self._knowledge_target_missed
                or not self._use_search
                or bool(self._web_evidence_fragments)
            )
            and not "".join(self.answer_fragments).strip()
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

        max_overlap = min(len(existing_text), len(final_text))
        for overlap in range(max_overlap, 0, -1):
            if existing_text.endswith(final_text[:overlap]):
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
        is_summarization = "summarization" in tags or metadata.get("purpose") == "summarization"

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
            self._model_generation_round += 1
            self._skipped_stream_chunks_tool_round = 0
            self._current_gen_has_tool_calls = False
            self._round_forwarded_chars = 0
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
        elif kind in {"on_custom_event", "on_chain_stream"}:
            async for item in self._on_task_runtime(event):
                yield item

    def log_session_diagnostic(self) -> None:
        """Emit end-of-session diagnostics for low/empty visible output."""
        visible_chars = self._normal_output_chars
        answer_text = "".join(self.answer_fragments)
        hints: list[str] = []

        if self.has_incomplete_model_round():
            hints.append(
                f"模型轮次未正常收尾: started={self._model_generation_round}, "
                f"ended={len(self._model_round_summaries)}, open={self._model_round_open}, "
                f"stream_exit={self._stream_exit_reason or 'unknown'}"
            )
        elif self._stream_exit_reason:
            hints.append(f"LangGraph 流退出原因: {self._stream_exit_reason}")

        if self._total_skipped_tool_chunks > 0 and not self._is_deep_research:
            hints.append(
                f"普通模式下工具轮共过滤流式正文 {self._total_skipped_tool_chunks} 片"
                f"（{self._model_generation_round} 轮模型中部分轮次仅含 tool_calls）"
            )
        if self._total_reasoning_chars > 0 and visible_chars < _LOW_VISIBLE_OUTPUT_WARN_CHARS:
            hints.append(
                f"思考流合计 {self._total_reasoning_chars} 字，不计入可见字符"
            )
        if self.timeout_reached:
            hints.append("流程因空闲/总超时提前收敛")
        if self.output_truncated:
            hints.append(f"命中普通模式输出长度上限 max_output_chars={self._max_output_chars}")
        if "html-ppt" in self._selected_skills and not self._html_ppt_succeeded:
            hints.append("已启用 html-ppt skill 但未成功 generate_html_ppt")
        if self._any_tool_executed and visible_chars < _LOW_VISIBLE_OUTPUT_WARN_CHARS:
            if not self._tools_invoked:
                hints.append("标记有工具执行但未记录工具名（异常）")
            elif "generate_html_ppt" not in self._tools_invoked and "html-ppt" in self._selected_skills:
                hints.append(
                    f"工具已执行 {self._tools_invoked}，缺少 generate_html_ppt"
                )
        if self._model_round_summaries:
            last = self._model_round_summaries[-1]
            if last.get("malformed_tool_call"):
                hints.append(
                    f"round={last.get('round')} 返回 finish_reason=tool_calls 但未提供可执行工具调用"
                )
            if not last.get("tool_names") and int(last.get("final_text_len") or 0) < 80:
                hints.append(
                    f"最后一轮(round={last.get('round')})无工具调用且 final_text 仅 "
                    f"{last.get('final_text_len')} 字，finish_reason={last.get('finish_reason')}"
                )

        preview = answer_text[:120].replace("\n", "\\n") if answer_text else ""
        round_brief = [
            (
                f"r{r.get('round')}:tools={r.get('tool_names') or '-'}"
                f",fwd={r.get('forwarded_chars', 0)}"
                f",skip={r.get('skipped_chunks', 0)}"
                f",final={r.get('final_text_len', 0)}"
                f",reason={r.get('finish_reason') or '-'}"
            )
            for r in self._model_round_summaries
        ]
        msg = (
            f"[NEAgentStreamHandler] 会话输出诊断 thread_id={self._thread_id} | "
            f"可见字符={visible_chars} chunk={self.chunk_count} "
            f"模型轮次={self._model_generation_round} "
            f"工具调用={self._tools_invoked or '无'} "
            f"过滤正文片={self._total_skipped_tool_chunks} "
            f"思考字={self._total_reasoning_chars} "
            f"timeout={self.timeout_reached} truncated={self.output_truncated} "
            f"stream_exit={self._stream_exit_reason or '-'} "
            f"incomplete_round={self.has_incomplete_model_round()} "
            f"preview={preview!r} | rounds=[{' | '.join(round_brief)}]"
        )
        if hints:
            msg += f" | 可能原因: {'; '.join(hints)}"

        if self.has_incomplete_model_round():
            logger.warning(msg)
        elif visible_chars < _LOW_VISIBLE_OUTPUT_WARN_CHARS and (
            self._any_tool_executed or self._model_generation_round > 0
        ):
            logger.warning(msg)
        else:
            logger.info(msg)

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
            msg = (
                "\n\n本轮已执行项目数据和知识库检索，但模型最终答案没有正常收尾，"
                "系统已停止继续等待。请重新发起本问题；若是水质超标判断，"
                "可指定点位、评价类别和需要比较的指标，系统会重新读取项目水质长表并按标准限值比较。"
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

        detail = (
            f"本轮共处理 {self.event_count} 个事件，生成 {self.chunk_count} 个输出片段"
            if not self.timeout_reached
            else f"流程已提前收敛：共处理 {self.event_count} 个事件，生成 {self.chunk_count} 个输出片段"
        )
        yield {
            "type": "event",
            "event": make_process_event(
                kind="answer",
                title="回答完成",
                detail=detail,
                level="success",
                metadata={
                    "event_count": self.event_count,
                    "chunk_count": self.chunk_count,
                    "timeout_reached": self.timeout_reached,
                    "output_truncated": self.output_truncated,
                },
            ),
        }

    async def _on_model_stream(self, event: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        content = event.get("data", {}).get("chunk", {})
        reasoning_text = extract_reasoning_content(content)
        if reasoning_text:
            self._round_reasoning_chars += len(reasoning_text)
            self._total_reasoning_chars += len(reasoning_text)
            yield {"type": "thinking", "content": reasoning_text}

        if not hasattr(content, "content") or not content.content:
            return

        raw_content_type = type(content.content).__name__
        chunk_text = stringify_message_content(content.content)
        if not chunk_text:
            logger.info("[NEAgentStreamHandler] 跳过空模型 chunk: raw_content_type=%s", raw_content_type)
            return

        if raw_content_type != "str":
            logger.info(
                "[NEAgentStreamHandler] 结构化模型 chunk 已转文本: raw_content_type=%s, text_len=%d",
                raw_content_type,
                len(chunk_text),
            )

        if hasattr(content, "tool_call_chunks") and content.tool_call_chunks:
            for tc_chunk in content.tool_call_chunks:
                tc_name = getattr(tc_chunk, "name", None) or ""
                tc_args = getattr(tc_chunk, "args", None) or ""
                if tc_name or tc_args:
                    if not self._current_gen_has_tool_calls:
                        self._current_gen_has_tool_calls = True
                        logger.info(
                            "[NEAgentStreamHandler] 检测到工具调用流: round=%d thread_id=%s "
                            "tool_hint=%r（后续正文 chunk 在普通模式下不转发）",
                            self._model_generation_round,
                            self._thread_id,
                            tc_name or tc_args[:40] if isinstance(tc_args, str) else tc_args,
                        )
                    break

        if self._current_gen_has_tool_calls and not self._is_deep_research:
            self._skipped_stream_chunks_tool_round += 1
            self._total_skipped_tool_chunks += 1
            return

        chunk_text, markup_thinking = self._split_thinking_markup(chunk_text)
        if markup_thinking:
            yield {"type": "thinking", "content": markup_thinking}
        if not chunk_text:
            return

        self.chunk_count += 1

        # 将 chunk 通过流式 token 解析器，替换 [IMAGE:xxx] 为真实 URL
        resolved_chunk = self._token_resolver.feed(chunk_text)
        # answer_fragments 存已替换后的内容，确保持久化历史时不含裸 token
        self.answer_fragments.append(resolved_chunk)

        # 检查 resolved_chunk 中是否仍有完整但未替换的 token（注册表缺失）
        if resolved_chunk and '[IMAGE:' in resolved_chunk:
            import re as _re_img
            unresolved = _re_img.findall(r'\[IMAGE:\d+\]', resolved_chunk)
            if unresolved:
                logger.warning(
                    "[StreamHandler] 流式输出中存在未替换 token %s，"
                    "注册表中无对应 URL（模型可能引用了历史旧 token）",
                    unresolved,
                )

        if self._is_deep_research:
            if not self._output_started_status_sent:
                self._output_started_status_sent = True
                yield {"type": "status", "content": "已开始生成回答..."}
            if resolved_chunk:
                yield {"type": "chunk", "content": resolved_chunk}
            return

        if self._max_output_chars > 0:
            remain = self._max_output_chars - self._normal_output_chars
            if remain <= 0:
                self.output_truncated = True
                yield {"type": "status", "content": "普通模式回答已达到长度上限，系统正在提前收敛输出。"}
                return
            if len(resolved_chunk) > remain:
                resolved_chunk = resolved_chunk[:remain]
                self.output_truncated = True

        self._normal_output_chars += len(resolved_chunk)
        self._round_forwarded_chars += len(resolved_chunk)

        if not self._output_started_status_sent:
            self._output_started_status_sent = True
            yield {"type": "status", "content": "已开始生成回答..."}

        if resolved_chunk:
            yield {"type": "chunk", "content": resolved_chunk}
        if self.output_truncated:
            yield {"type": "status", "content": "普通模式回答已达到长度上限，系统正在提前收敛输出。"}

    async def _on_model_end(self, event: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        skipped_this_round = self._skipped_stream_chunks_tool_round
        if skipped_this_round:
            self._skipped_stream_chunks_tool_round = 0

        event_output = (event.get("data", {}) or {}).get("output")
        final_text = extract_chat_model_output_text(event_output)
        tool_names = _extract_tool_call_names(event_output)
        finish_reason = _extract_finish_reason(event_output)
        malformed_tool_call = finish_reason == "tool_calls" and not tool_names and not self._current_gen_has_tool_calls
        if malformed_tool_call:
            self._malformed_tool_call_rounds += 1
        existing_text = "".join(self.answer_fragments)
        missing_text = self._compute_missing_tail(existing_text, final_text)

        round_summary = {
            "round": self._model_generation_round,
            "tool_names": tool_names,
            "finish_reason": finish_reason,
            "final_text_len": len(final_text),
            "forwarded_chars": self._round_forwarded_chars,
            "skipped_chunks": skipped_this_round,
            "reasoning_chars": self._round_reasoning_chars,
            "missing_recovered_len": len(missing_text),
            "had_tool_calls": bool(tool_names) or self._current_gen_has_tool_calls,
            "malformed_tool_call": malformed_tool_call,
        }
        self._model_round_summaries.append(round_summary)
        self._model_round_open = False

        logger.info(
            "[NEAgentStreamHandler] 模型轮次结束: round=%d thread_id=%s "
            "tools=%s finish_reason=%s final_text_len=%d forwarded_chars=%d "
            "skipped_chunks=%d reasoning_chars=%d missing_recovered_len=%d",
            self._model_generation_round,
            self._thread_id,
            tool_names or "-",
            finish_reason or "-",
            len(final_text),
            self._round_forwarded_chars,
            skipped_this_round,
            self._round_reasoning_chars,
            len(missing_text),
        )
        if skipped_this_round and not self._is_deep_research:
            logger.info(
                "[NEAgentStreamHandler] round=%d 工具轮过滤说明: 未转发流式正文 %d 片"
                "（普通模式预期行为；最终交付依赖 tool 结果或 missing_text 补发）",
                self._model_generation_round,
                skipped_this_round,
            )
        if (
            not self._is_deep_research
            and not tool_names
            and not self._current_gen_has_tool_calls
            and len(final_text) < 80
            and self._round_forwarded_chars < 80
        ):
            logger.warning(
                "[NEAgentStreamHandler] round=%d 疑似空输出收尾: final_text_len=%d "
                "forwarded_chars=%d finish_reason=%s malformed_tool_call=%s preview=%r",
                self._model_generation_round,
                len(final_text),
                self._round_forwarded_chars,
                finish_reason or "-",
                malformed_tool_call,
                final_text[:80].replace("\n", "\\n"),
            )

        # 刷新流式解析器缓冲区（处理末尾可能残留的 token 片段）
        flushed = self._token_resolver.flush()
        if flushed:
            self.answer_fragments.append(flushed)
            if not self._output_started_status_sent:
                self._output_started_status_sent = True
                yield {"type": "status", "content": "已开始生成回答..."}
            yield {"type": "chunk", "content": flushed}

        if round_summary["had_tool_calls"] and not self._is_deep_research:
            return

        if missing_text:
            missing_text, markup_thinking = self._split_thinking_markup(missing_text)
            if markup_thinking:
                yield {"type": "thinking", "content": markup_thinking}
            if not missing_text:
                return
            # missing_text 未经过流式解析器，直接做全量替换
            resolved_missing = resolve_image_tokens(missing_text)
            # 检查是否仍有未替换 token（说明模型引用了注册表中不存在的 token）
            import re as _re_tok
            unresolved = _re_tok.findall(r'\[IMAGE:\d+\]', resolved_missing)
            if unresolved:
                logger.warning(
                    "[StreamHandler] missing_text 仍含未替换 token %s，"
                    "可能模型引用了历史会话中的旧 token",
                    unresolved,
                )
            self.answer_fragments.append(resolved_missing)
            if not self._output_started_status_sent:
                self._output_started_status_sent = True
                yield {"type": "status", "content": "已开始生成回答..."}
            for recovered_chunk in iter_recovered_text_chunks(resolved_missing):
                self.chunk_count += 1
                self._normal_output_chars += len(recovered_chunk)
                self._round_forwarded_chars += len(recovered_chunk)
                yield {"type": "chunk", "content": recovered_chunk}

    async def _on_tool_start(self, event: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        tool_name = event.get("name")
        if tool_name:
            self._tools_invoked.append(str(tool_name))
        self._tool_started_at[tool_name] = time.monotonic()
        data = event.get("data", {}) or {}
        tool_input = data.get("input", {})

        if isinstance(tool_input, str):
            try:
                tool_input = json.loads(tool_input)
            except Exception:
                tool_input = {"query": tool_input}

        if tool_name == "multi_kb_search":
            q = tool_input.get("query", "未知")
            kb_ids = tool_input.get("kb_ids")
            from backend.application.tools.providers.multi_kb_search_tool import DEFAULT_KB_ID, KB_LABELS, PAPER_KB_ID

            effective_kb_ids = kb_ids if kb_ids else (self._kb_ids or [DEFAULT_KB_ID, PAPER_KB_ID])
            kb_names = [KB_LABELS.get(kid, kid) for kid in effective_kb_ids]
            kb_names_str = "、".join(kb_names)
            title = "检索专业知识库"
            detail = f"正在{kb_names_str}中检索: '{q}'"
        elif tool_name in {"web_search", "duckduckgo_search"}:
            q = tool_input.get("query", "未知")
            title = "联网搜索"
            detail = f"正在搜索全网资讯: '{q}'"
        elif tool_name == "academic_search":
            q = tool_input.get("query", "未知")
            title = "学术文献检索"
            detail = f"正在检索学术文献: '{q}'"
        elif tool_name == "task":
            desc = tool_input.get("description", "未知任务")
            title = "子智能体并行执行"
            detail = f"委派子任务: {desc}"
        elif tool_name == "save_deep_research_report":
            title = "生成研究报告"
            detail = "正在保存研究报告并上传 MinIO"
        else:
            title = "工具调用"
            detail = f"使用工具: {tool_name}"

        yield {
            "type": "event",
            "event": make_process_event(
                kind="tool_start",
                title=title,
                detail=detail,
                tool=tool_name,
                level="active",
                metadata={"input": tool_input},
            ),
        }

    async def _on_tool_end(self, event: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        tool_name = event.get("name")
        elapsed_ms = None
        started_at = self._tool_started_at.pop(tool_name, None)
        if started_at is not None:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)

        if not self._is_deep_research:
            self._any_tool_executed = True

        data = event.get("data", {}) or {}
        raw_output = data.get("output") or data.get("content") or data.get("result")
        summary = summarize_tool_output(raw_output)
        result_metadata: dict[str, Any] = {"source": "tool_output"}

        if not self._is_deep_research:
            if tool_name in _GROUNDWATER_PROJECT_EVIDENCE_TOOLS:
                evidence = self._stringify_evidence(raw_output, max_chars=12000)
                self._append_evidence(
                    self._project_evidence_fragments,
                    f"<project_tool_result name=\"{tool_name}\">\n{evidence}\n</project_tool_result>",
                    max_items=4,
                )
            elif tool_name == "multi_kb_search":
                evidence = self._stringify_evidence(raw_output, max_chars=10000)
                self._knowledge_target_missed = (
                    "<target_document_match>false</target_document_match>" in evidence
                )
                self._append_evidence(
                    self._retrieved_evidence_fragments,
                    f"<knowledge_tool_result name=\"multi_kb_search\">\n{evidence}\n</knowledge_tool_result>",
                    max_items=4,
                )
                if "知识库检索次数已达上限" in evidence and self._project_evidence_fragments:
                    self._writer_handoff_ready = True
            elif tool_name in {"web_search", "duckduckgo_search"}:
                evidence = self._stringify_evidence(raw_output, max_chars=10000)
                self._append_evidence(
                    self._web_evidence_fragments,
                    f"<web_tool_result name=\"{tool_name}\">\n{evidence}\n</web_tool_result>",
                    max_items=2,
                )

        if tool_name in {"web_search", "duckduckgo_search"}:
            result_metadata.update(extract_web_search_metadata(raw_output))
        elif tool_name in {"save_deep_research_report", "generate_html_ppt"}:
            result_metadata.update(extract_report_metadata(raw_output))

        status_title = "工具调用完成"
        status_detail = f"{tool_name} 调用完成"

        if tool_name == "multi_kb_search":
            if "<target_document_match>false</target_document_match>" in self._stringify_evidence(raw_output, max_chars=10000):
                status_title = "目标标准未命中"
                status_detail = "知识库仅返回相关资料，将继续使用联网搜索核验目标标准"
            else:
                status_title = "知识库检索完成"
                status_detail = "已成功获取相关的知识库文档"
        elif tool_name in {"web_search", "duckduckgo_search"}:
            status_title = "网络搜索完成"
            status_detail = "已获取网页参考资讯"
        elif tool_name == "academic_search":
            status_title = "文献检索完成"
            status_detail = "已获取学术论文检索结果"
        elif tool_name == "task":
            status_title = "子任务完成"
            task_meta = extract_task_result_metadata(raw_output)
            if task_meta:
                result_metadata.update(task_meta)
                preview = str(task_meta.get("result_preview") or "").strip()
                if preview:
                    summary = summarize_tool_output(preview, max_length=500)
                query_count = len(task_meta.get("search_queries") or []) if isinstance(task_meta.get("search_queries"), list) else 0
                url_count = len(task_meta.get("urls") or []) if isinstance(task_meta.get("urls"), list) else 0
                if query_count or url_count:
                    status_detail = f"子任务执行完成（检索关键词 {query_count} 条，来源链接 {url_count} 条）"
                else:
                    status_detail = "子任务执行并获取结果完成"
            if self._is_deep_research and not self.deep_research_grace_applied:
                self.deep_research_grace_applied = True
        elif tool_name == "save_deep_research_report":
            artifact_path = str(result_metadata.get("artifact_path") or "").strip()
            if artifact_path:
                status_title = "研究报告已保存"
                status_detail = "报告已写入文档并生成可访问链接"
                self.report_saved = True
            else:
                status_title = "研究报告保存失败"
                status_detail = "未获取到 artifact_path，稍后将触发自动兜底归档"
                self.report_saved = False
        elif tool_name == "generate_html_ppt":
            artifact_path = str(result_metadata.get("artifact_path") or "").strip()
            if artifact_path:
                status_title = "HTML PPT 已生成"
                status_detail = "演示文稿已按用户隔离流程保存并生成可预览链接"
            else:
                status_title = "HTML PPT 生成失败"
                status_detail = "未获取到 HTML 工件信息"

        tool_end_warning = (tool_name == "save_deep_research_report" and not self.report_saved) or (
            tool_name == "generate_html_ppt" and not str(result_metadata.get("artifact_path") or "").strip()
        )

        yield {
            "type": "event",
            "event": make_process_event(
                kind="tool_end",
                title=status_title,
                detail=status_detail,
                tool=tool_name,
                level="warning" if tool_end_warning else "success",
                duration_ms=elapsed_ms,
            ),
        }

        should_emit_tool_result = tool_name not in SUPPRESSED_TOOL_RESULT_TOOLS
        if summary and should_emit_tool_result:
            if tool_name in {"multi_kb_search", "web_search", "duckduckgo_search", "academic_search", "task"}:
                self.collected_research_notes.append(f"- [{tool_name}] {summary}")
            tool_result_detail = summary
            if tool_name in {"web_search", "duckduckgo_search"}:
                total = result_metadata.get("total_results")
                url_count = len(result_metadata.get("urls") or [])
                if isinstance(total, int) and url_count:
                    tool_result_detail = f"共 {total} 条结果，{url_count} 个网页来源"
                elif url_count:
                    tool_result_detail = f"共 {url_count} 个网页来源"
            yield {
                "type": "event",
                "event": make_process_event(
                    kind="tool_result",
                    title="结果摘要",
                    detail=tool_result_detail,
                    tool=tool_name,
                    level="info",
                    duration_ms=elapsed_ms,
                    summary=summary,
                    metadata=result_metadata,
                ),
            }

        if tool_name in {"save_deep_research_report", "generate_html_ppt"}:
            artifact_path = str(result_metadata.get("artifact_path") or "").strip()
            if artifact_path and artifact_path not in self.sent_artifacts:
                self.sent_artifacts.add(artifact_path)
                artifact_title = str(
                    result_metadata.get("title")
                    or ("深度研究报告" if tool_name == "save_deep_research_report" else "HTML PPT")
                )
                artifact_metadata = await build_registered_artifact_metadata(
                    internal_thread_id=self._thread_id,
                    artifact_path=artifact_path,
                    title=artifact_title,
                    minio_object=str(result_metadata.get("minio_object") or "") or None,
                )
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
                if tool_name == "generate_html_ppt":
                    # 模型在纯工具调用回合常不发可见正文，前端会显示 0 chunk；补一句落气泡的说明
                    user_ack = f"已生成 HTML 演示文稿「{artifact_title}」，请在工件/附件区域打开预览或下载。\n"
                    self.answer_fragments.append(user_ack)
                    self.chunk_count += 1
                    self._normal_output_chars += len(user_ack)
                    self._html_ppt_succeeded = True
                    yield {"type": "chunk", "content": user_ack}
            elif tool_name == "generate_html_ppt" and not artifact_path:
                fail_msg = (
                    f"{status_title}：{status_detail}。\n\n"
                    "请先 `read_local_skill_file(\"html-ppt\")` 核对 template/theme 名称与 `slides_outline` 格式，"
                    "再重试 `generate_html_ppt`。\n"
                )
                self.answer_fragments.append(fail_msg)
                self.chunk_count += 1
                self._normal_output_chars += len(fail_msg)
                yield {"type": "chunk", "content": fail_msg}

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

    async def _on_chain_end(self, event: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        chain_name = event.get("name", "chain")
        if "task" in str(chain_name).lower():
            yield {
                "type": "event",
                "event": make_process_event(
                    kind="task_end",
                    title="任务完成",
                    detail=f"子任务执行完成: {chain_name}",
                    tool=chain_name,
                    level="success",
                ),
            }
        elif str(chain_name) == "ne-full-feature":
            yield {"type": "status", "content": "回答生成完成"}

    async def _on_task_runtime(self, event: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        task_runtime_event = extract_task_runtime_event_payload(event)
        if task_runtime_event is None:
            return

        runtime_type = str(task_runtime_event.get("type", "")).strip()
        task_id = str(task_runtime_event.get("task_id", "")).strip() or None
        description = str(task_runtime_event.get("description", "")).strip()

        if runtime_type.startswith("task_"):
            self.deep_research_last_task_runtime_at = time.monotonic()
            if task_id and runtime_type in {"task_started", "task_running", "task_tool_start", "task_tool_end"}:
                self.deep_research_active_task_ids.add(task_id)
            if task_id and runtime_type in {"task_completed", "task_failed", "task_timed_out"}:
                self.deep_research_active_task_ids.discard(task_id)

        runtime_metadata = build_task_runtime_metadata(
            task_runtime_event,
            task_id=task_id,
            description=description or None,
        )

        if runtime_type == "task_started":
            yield {
                "type": "event",
                "event": make_process_event(
                    kind="task_start",
                    title="子智能体已启动",
                    detail=description or f"任务已启动: {task_id or 'unknown'}",
                    tool="task",
                    level="active",
                    metadata=runtime_metadata,
                ),
            }
        elif runtime_type == "task_running":
            raw_message = task_runtime_event.get("message", "")
            message = extract_task_message_text(raw_message)
            message_text = str(task_runtime_event.get("message_text", "")).strip()
            if message_text:
                message = message_text
            if message:
                yield {
                    "type": "event",
                    "event": make_process_event(
                        kind="task_progress",
                        title="子智能体进展",
                        detail=message,
                        tool="task",
                        level="info",
                        metadata={
                            **runtime_metadata,
                            "message_index": task_runtime_event.get("message_index"),
                            "total_messages": task_runtime_event.get("total_messages"),
                        },
                    ),
                }
        elif runtime_type in {"task_tool_start", "task_tool_end"}:
            tool_name = str(task_runtime_event.get("tool", "")).strip() or "unknown_tool"
            detail = str(task_runtime_event.get("detail", "")).strip()
            summary = str(task_runtime_event.get("summary", "")).strip()
            event_kind = "tool_start" if runtime_type == "task_tool_start" else "tool_result"
            yield {
                "type": "event",
                "event": make_process_event(
                    kind=event_kind,
                    title="子智能体工具调用" if runtime_type == "task_tool_start" else "子智能体工具结果",
                    detail=detail or (f"{tool_name} started" if runtime_type == "task_tool_start" else f"{tool_name} completed"),
                    tool=tool_name,
                    level="active" if runtime_type == "task_tool_start" else "info",
                    summary=summary if runtime_type == "task_tool_end" and summary else None,
                    metadata={
                        **runtime_metadata,
                        "tool": tool_name,
                        "detail": detail,
                        "runtime_type": runtime_type,
                    },
                ),
            }
        elif runtime_type == "task_completed":
            result_preview = str(task_runtime_event.get("result_preview", "")).strip()
            result_text = result_preview or summarize_tool_output(task_runtime_event.get("result"), max_length=2000)
            yield {
                "type": "event",
                "event": make_process_event(
                    kind="task_end",
                    title="子智能体已完成",
                    detail=result_text or "子任务执行完成",
                    tool="task",
                    level="success",
                    metadata={
                        **runtime_metadata,
                        "result": result_text,
                        "runtime_type": runtime_type,
                    },
                ),
            }
        elif runtime_type in {"task_failed", "task_timed_out"}:
            error = str(task_runtime_event.get("error", "")).strip() or runtime_type
            yield {
                "type": "event",
                "event": make_process_event(
                    kind="task_end",
                    title="子智能体执行异常",
                    detail=error,
                    tool="task",
                    level="warning",
                    metadata={
                        **runtime_metadata,
                        "runtime_type": runtime_type,
                        "error": error,
                    },
                ),
            }
