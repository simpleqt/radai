"""模型流式/收尾事件处理实现(拆自 NEAgentStreamHandler,首参 self 为 handler 实例)。"""

from __future__ import annotations

from backend.application.agent_executors.image_url_registry import resolve_image_tokens
from backend.application.agent_executors.stream_utils import (
    extract_chat_model_output_text,
    extract_reasoning_content,
    iter_recovered_text_chunks,
    stringify_message_content,
)
from backend.application.event_stream.thinking_filter import split_thinking_segments
from backend.config.loggers import get_logger
from typing import Any, AsyncIterator
from backend.application.agent_executors.stream_tool_evidence import (
    _LOW_VISIBLE_OUTPUT_WARN_CHARS,
    _extract_finish_reason,
    _extract_tool_call_names,
)


logger = get_logger(__name__)


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

    # Keep the model's raw visible deltas for this generation round.  The
    # model-end payload contains only the current round, while
    # ``answer_fragments`` also contains visible text emitted before tool
    # calls in earlier rounds.  Comparing the final payload with the whole
    # conversation turn makes a streamed prefix look unrelated and replays
    # the complete answer, which duplicates the output in the browser.
    self._round_streamed_visible_text += chunk_text

    self.chunk_count += 1

    # 将 chunk 通过流式 token 解析器，替换 [IMAGE:xxx] 为真实 URL
    resolved_chunk = self._token_resolver.feed(chunk_text)

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
        # 深研无长度上限,直播与持久化一致存全量。
        self.answer_fragments.append(resolved_chunk)
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

    # cap 判定必须在 append 之前:answer_fragments 只存实际转发的(可能已截断)
    # 文本,否则超限尾巴会经 finalize 的 message.replace / 兜底写入持久化,
    # 造成直播(截断)与刷新后历史(超限全文)发散。
    self.answer_fragments.append(resolved_chunk)
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
    (
        final_visible_text,
        final_markup_thinking,
        final_in_thinking,
        final_carry,
    ) = split_thinking_segments(final_text, False, "")
    if final_carry and not final_in_thinking:
        final_visible_text += final_carry
    # 工具调用轮的「幽灵正文」对账：部分网关（如 glm）在发起工具调用前把一段
    # 预演文本（常见为 JSON）灌进 content 流，这段文本已被实时转发到浏览器，
    # 但不会进入该轮的留存消息（finish_reason=tool_calls 的最终 content 不含它）。
    # 结果是直播气泡多出一段刷新后会消失的内容。这里按留存文本收敛：
    # 剪掉 answer_fragments 里的幽灵尾巴并下发 message.replace，让直播与
    # 历史回放展示同一份文本。
    ghost_round = (
        not self._is_deep_research
        and (bool(tool_names) or self._current_gen_has_tool_calls)
        and bool(self._round_streamed_visible_text)
        and self._round_streamed_visible_text != final_visible_text
        and self._round_streamed_visible_text.startswith(final_visible_text)
    )
    if ghost_round:
        ghost_len = len(self._round_streamed_visible_text) - len(final_visible_text)
        base = min(getattr(self, "_round_fragment_base", 0), len(self.answer_fragments))
        kept = self.answer_fragments[:base]
        self.answer_fragments = kept + (
            [final_visible_text] if final_visible_text else []
        )
        # 幽灵文本可能有尾巴滞留在 token 解析器缓冲里（如 "[…"），一并丢弃，
        # 防止本函数末尾的常规 flush 把它重新追加回来
        self._token_resolver.flush()
        self._round_streamed_visible_text = final_visible_text
        corrected = "".join(self.answer_fragments)
        logger.info(
            "[NEAgentStreamHandler] 工具轮幽灵正文已收敛: round=%d "
            "trimmed_chars=%d final_len=%d",
            self._model_generation_round,
            ghost_len,
            len(final_visible_text),
        )
        yield {"type": "message.replace", "content": corrected}

    # Most gateways return only the current model generation here, while a
    # few writer-style stages return an aggregate payload that also
    # contains text emitted before this round.  Compute both valid recovery
    # tails and emit the shorter one: this preserves unstreamed suffixes
    # without replaying either a previous tool round or an aggregate writer
    # prefix.
    round_missing_text = self._compute_missing_tail(
        self._round_streamed_visible_text,
        final_visible_text,
    )
    aggregate_missing_text = self._compute_missing_tail(
        "".join(self.answer_fragments),
        final_visible_text,
    )
    missing_text = min(
        (round_missing_text, aggregate_missing_text),
        key=len,
    )
    if malformed_tool_call and "<tool_call" in final_text.lower():
        # Some OpenAI-compatible gateways return an unparsed tool call as
        # message text. It is control-plane data, never an answer.
        missing_text = ""

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

    # 普通模式已命中长度上限时,收尾阶段不得再补发文本(flush 尾巴 / missing
    # 尾巴),否则超限内容经历史持久化让刷新前后发散(R3-EX-7)。
    cap_reached = (not self._is_deep_research) and self.output_truncated

    # 刷新流式解析器缓冲区（处理末尾可能残留的 token 片段）
    flushed = "" if cap_reached else self._token_resolver.flush()
    if flushed:
        self.answer_fragments.append(flushed)
        if not self._output_started_status_sent:
            self._output_started_status_sent = True
            yield {"type": "status", "content": "已开始生成回答..."}
        yield {"type": "chunk", "content": flushed}

    if round_summary["had_tool_calls"] and not self._is_deep_research:
        return

    if final_markup_thinking and self._round_reasoning_chars == 0:
        self._round_reasoning_chars += len(final_markup_thinking)
        self._total_reasoning_chars += len(final_markup_thinking)
        yield {"type": "thinking", "content": final_markup_thinking}

    if missing_text and cap_reached:
        # 已达上限,丢弃 model-end 的未流出尾巴,保持 cap 语义与直播一致。
        missing_text = ""

    if missing_text:
        # final_visible_text has already had thinking markup removed before
        # overlap detection, so a full model-end payload cannot replay an
        # answer that was delivered through streaming callbacks.
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
