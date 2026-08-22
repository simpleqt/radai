"""Streaming/tool-call compatible adapter for OpenAI-compatible gateways.

Some gateways expose reasoning models through an OpenAI-compatible streaming API,
but provider-specific reasoning fields are not preserved by the default
``langchain_openai.ChatOpenAI`` adapter. This provider keeps those fields on
streaming deltas and non-streaming responses, and mirrors ``reasoning_content``
back into assistant messages on subsequent turns when needed. It also keeps
normal answer text streaming while safely repairing malformed tool-call streams.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.outputs.chat_generation import merge_chat_generation_chunks
from langchain_core.tracers._streaming import _StreamingCallbackHandler
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import (
    _convert_delta_to_message_chunk,
    _create_usage_metadata,
)

from backend.core.tools.tool_arguments import parse_malformed_tool_args

logger = logging.getLogger(__name__)

_REASONING_KEYS = (
    "reasoning_content",
    "reasoning",
    "reasoningContent",
    "reasoning_text",
    "reasoningText",
    "reasoning_details",
    "thinking",
    "thinking_content",
    "thinkingContent",
    "thought",
    "thoughts",
    "analysis",
)


def _best_effort_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_best_effort_text(item) for item in value]
        return "".join(part for part in parts if part)
    if isinstance(value, Mapping):
        for key in ("text", "content", "reasoning", "reasoning_content", "summary"):
            nested = value.get(key)
            if nested is None:
                continue
            text = _best_effort_text(nested)
            if text:
                return text
        try:
            return json.dumps(dict(value), ensure_ascii=False)
        except TypeError:
            return str(value)
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def _extract_reasoning_payload(data: Mapping[str, Any] | None) -> tuple[str | None, dict[str, Any]]:
    if not isinstance(data, Mapping):
        return None, {}

    raw_hits: dict[str, Any] = {}
    texts: list[str] = []
    for key in _REASONING_KEYS:
        if key not in data:
            continue
        value = data.get(key)
        if value in (None, "", [], {}):
            continue
        raw_hits[key] = value
        text = _best_effort_text(value)
        if text:
            if text not in texts:
                texts.append(text)

    reasoning_text = "".join(texts) if texts else None
    return reasoning_text, raw_hits


def _with_reasoning_content(
    message: AIMessage | AIMessageChunk,
    reasoning_text: str | None,
    raw_reasoning: dict[str, Any] | None = None,
):
    if not reasoning_text and not raw_reasoning:
        return message

    additional_kwargs = dict(message.additional_kwargs)
    if raw_reasoning:
        sanitized_reasoning = dict(raw_reasoning)
        sanitized_reasoning.pop("reasoning_content", None)
        additional_kwargs.update(sanitized_reasoning)
    if reasoning_text:
        existing = additional_kwargs.get("reasoning_content")
        if isinstance(existing, str) and existing and existing != reasoning_text:
            combined = f"{existing}{reasoning_text}"
        else:
            combined = reasoning_text
        additional_kwargs["reasoning_content"] = combined
    return message.model_copy(update={"additional_kwargs": additional_kwargs})


def _restore_reasoning_content(payload_msg: dict[str, Any], orig_msg: AIMessage) -> None:
    reasoning_content = orig_msg.additional_kwargs.get("reasoning_content")
    if reasoning_content is not None:
        payload_msg["reasoning_content"] = reasoning_content


def _repair_invalid_tool_calls(message: AIMessage | AIMessageChunk):
    """Promote losslessly repairable provider calls to executable tool calls."""
    invalid_calls = list(getattr(message, "invalid_tool_calls", None) or [])
    tool_calls: list[dict[str, Any]] = []
    normalized = 0
    for call in getattr(message, "tool_calls", None) or []:
        if not isinstance(call, dict):
            continue
        parsed_args = parse_malformed_tool_args(call.get("args"))
        if parsed_args is not None and parsed_args != call.get("args"):
            tool_calls.append({**call, "args": parsed_args})
            normalized += 1
        else:
            tool_calls.append(call)

    if not invalid_calls and not normalized:
        return message
    known_ids = {
        str(call.get("id"))
        for call in tool_calls
        if isinstance(call, dict) and call.get("id")
    }
    recovered = 0
    duplicate = 0
    remaining: list[dict[str, Any]] = []
    for invalid in invalid_calls:
        if not isinstance(invalid, dict):
            remaining.append(invalid)
            continue
        tool_name = str(invalid.get("name") or "").strip()
        tool_call_id = str(invalid.get("id") or "").strip()
        parsed_args = parse_malformed_tool_args(invalid.get("args"))
        if not tool_name or not tool_call_id or parsed_args is None:
            remaining.append(invalid)
            continue
        if tool_call_id in known_ids:
            duplicate += 1
            continue
        tool_calls.append(
            {
                "name": tool_name,
                "args": parsed_args,
                "id": tool_call_id,
                "type": "tool_call",
            }
        )
        known_ids.add(tool_call_id)
        recovered += 1

    if not recovered and not duplicate and not normalized:
        return message

    update: dict[str, Any] = {
        "tool_calls": tool_calls,
        "invalid_tool_calls": remaining,
    }
    if isinstance(message, AIMessageChunk):
        update["tool_call_chunks"] = [
            {
                "name": call.get("name"),
                "args": json.dumps(call.get("args") or {}, ensure_ascii=False),
                "id": call.get("id"),
                "index": index,
                "type": "tool_call_chunk",
            }
            for index, call in enumerate(tool_calls)
            if isinstance(call, dict)
        ]
    logger.info(
        "[ReasoningCompatibleChatOpenAI] recovered %d malformed tool call(s); "
        "normalized %d executable call(s); removed %d duplicate invalid call(s); "
        "unrecoverable=%d",
        recovered,
        normalized,
        duplicate,
        len(remaining),
    )
    return message.model_copy(update=update)


class PatchedDeepSeekOpenAI(ChatOpenAI):
    """ChatOpenAI variant that preserves provider-specific reasoning fields."""

    def _should_stream(
        self,
        *,
        async_api: bool,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> bool:
        """Keep text streaming enabled while handling broken tool streams below."""
        handlers = run_manager.handlers if run_manager else []
        callback_streaming = any(
            isinstance(handler, _StreamingCallbackHandler) for handler in handlers
        )
        if (
            async_api
            and self.disable_streaming == "tool_calling"
            and kwargs.get("tools")
            and (kwargs.get("stream") is True or callback_streaming)
        ):
            return True
        return super()._should_stream(
            async_api=async_api,
            run_manager=run_manager,
            **kwargs,
        )

    async def _astream_provider(self, *args: Any, **kwargs: Any):
        async for chunk in super()._astream(*args, **kwargs):
            yield chunk

    @staticmethod
    def _has_tool_call_delta(chunk: ChatGenerationChunk) -> bool:
        message = chunk.message
        return bool(
            getattr(message, "tool_call_chunks", None)
            or getattr(message, "tool_calls", None)
            or getattr(message, "invalid_tool_calls", None)
        )

    @staticmethod
    def _has_executable_tool_call(chunks: list[ChatGenerationChunk]) -> bool:
        if not chunks:
            return False
        merged = merge_chat_generation_chunks(chunks)
        if merged is None:
            return False
        return any(
            str(tool_call.get("name") or "").strip()
            for tool_call in getattr(merged.message, "tool_calls", None) or []
            if isinstance(tool_call, dict)
        )

    @staticmethod
    def _finish_reason(chunk: ChatGenerationChunk) -> str:
        generation_info = chunk.generation_info or {}
        reason = generation_info.get("finish_reason") or generation_info.get("stop_reason")
        if reason:
            return str(reason)
        metadata = getattr(chunk.message, "response_metadata", None) or {}
        return str(metadata.get("finish_reason") or metadata.get("stop_reason") or "")

    @staticmethod
    def _to_generation_chunk(generation: ChatGeneration) -> ChatGenerationChunk:
        message = _repair_invalid_tool_calls(generation.message)
        if isinstance(message, AIMessageChunk):
            message_chunk = message
        else:
            message_chunk = AIMessageChunk(
                content=message.content,
                additional_kwargs=dict(message.additional_kwargs),
                response_metadata=dict(message.response_metadata),
                id=message.id,
                name=message.name,
                tool_calls=list(message.tool_calls),
                invalid_tool_calls=list(message.invalid_tool_calls),
                usage_metadata=message.usage_metadata,
            )
        return ChatGenerationChunk(
            message=message_chunk,
            generation_info=generation.generation_info,
        )

    async def _astream(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ):
        """Stream answer text, retrying only malformed tool-call streams.

        ``disable_streaming='tool_calling'`` normally makes every agent round
        non-streaming because bound tools are present even on the final answer
        round. Buffer only until the provider reveals whether the response is
        text or a tool call. A malformed tool-call stream is retried once with
        a regular completion so the graph still receives a complete tool call.
        """
        guarded_tool_stream = self.disable_streaming == "tool_calling" and bool(
            kwargs.get("tools")
        )
        if not guarded_tool_stream:
            async for chunk in self._astream_provider(
                messages,
                stop=stop,
                run_manager=run_manager,
                **kwargs,
            ):
                yield chunk
            return

        buffered: list[ChatGenerationChunk] = []
        classified = False
        saw_tool_delta = False
        finish_reason = ""
        provider_chunk_count = 0
        visible_chunk_count = 0
        late_tool_chunks: list[ChatGenerationChunk] = []

        logger.info(
            "[ReasoningCompatibleChatOpenAI] tool-bound model round using provider streaming"
        )

        async for chunk in self._astream_provider(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        ):
            provider_chunk_count += 1
            finish_reason = self._finish_reason(chunk) or finish_reason
            has_tool_delta = self._has_tool_call_delta(chunk)
            saw_tool_delta = saw_tool_delta or has_tool_delta

            content_text = _best_effort_text(chunk.message.content)
            has_visible_text = bool(content_text.strip())
            if has_visible_text:
                visible_chunk_count += 1
                if visible_chunk_count == 1:
                    logger.info(
                        "[ReasoningCompatibleChatOpenAI] first visible provider delta: chars=%d",
                        len(content_text),
                    )

            has_reasoning = any(
                bool(chunk.message.additional_kwargs.get(k))
                for k in _REASONING_KEYS
            ) if chunk.message and getattr(chunk.message, "additional_kwargs", None) else False

            if not classified:
                if has_reasoning:
                    yield chunk
                    continue

                buffered.append(chunk)
                if has_tool_delta:
                    continue
                if has_visible_text:
                    classified = True
                    for pending in buffered:
                        yield pending
                    buffered.clear()
                continue

            if has_tool_delta or late_tool_chunks:
                # Some reasoning providers emit a short visible preamble and
                # only then start a tool call.  Once the first tool delta is
                # observed, buffer the rest of the round so malformed JSON can
                # be repaired after all argument fragments have arrived.
                late_tool_chunks.append(chunk)
                continue

            yield chunk

        if classified:
            if finish_reason == "tool_calls" and late_tool_chunks:
                merged = merge_chat_generation_chunks(late_tool_chunks)
                if merged is not None:
                    repaired_message = _repair_invalid_tool_calls(merged.message)
                    repaired = merged.model_copy(update={"message": repaired_message})
                    if self._has_executable_tool_call([repaired]):
                        yield repaired
                        return
                logger.warning(
                    "[ReasoningCompatibleChatOpenAI] malformed late tool call after "
                    "visible text; retrying this model round without streaming"
                )
                retry_kwargs = dict(kwargs)
                retry_kwargs["stream"] = False
                result = await super()._agenerate(
                    messages,
                    stop=stop,
                    run_manager=run_manager,
                    **retry_kwargs,
                )
                for generation in result.generations:
                    yield self._to_generation_chunk(generation)
                return
            if late_tool_chunks:
                # 出现过工具增量,但本轮以 stop/length 而非 tool_calls 收尾:
                # 这些被缓冲的块其实是工具增量之后继续输出的正文与末尾 usage,
                # 不补吐会让答案在工具片段处静默截断(R4-LM-4)。按序补吐。
                for pending in late_tool_chunks:
                    yield pending
                late_tool_chunks.clear()
            logger.info(
                "[ReasoningCompatibleChatOpenAI] provider stream completed: "
                "chunks=%d visible_chunks=%d finish_reason=%s",
                provider_chunk_count,
                visible_chunk_count,
                finish_reason or "-",
            )
            return

        if finish_reason == "tool_calls":
            if saw_tool_delta:
                merged = merge_chat_generation_chunks(buffered)
                if merged is not None:
                    repaired_message = _repair_invalid_tool_calls(merged.message)
                    repaired = merged.model_copy(update={"message": repaired_message})
                    if self._has_executable_tool_call([repaired]):
                        # Tool-call deltas are buffered already. Yielding one
                        # repaired aggregate prevents a later chunk merge from
                        # recreating the same InvalidToolCall.
                        yield repaired
                        return
            logger.warning(
                "[ReasoningCompatibleChatOpenAI] malformed streamed tool call; "
                "retrying this model round without streaming"
            )
            retry_kwargs = dict(kwargs)
            retry_kwargs["stream"] = False
            result = await super()._agenerate(
                messages,
                stop=stop,
                run_manager=run_manager,
                **retry_kwargs,
            )
            for generation in result.generations:
                yield self._to_generation_chunk(generation)
            return

        for pending in buffered:
            yield pending

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        original_messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        payload_messages = payload.get("messages", [])

        if len(payload_messages) == len(original_messages):
            for payload_msg, orig_msg in zip(payload_messages, original_messages):
                if payload_msg.get("role") == "assistant" and isinstance(orig_msg, AIMessage):
                    _restore_reasoning_content(payload_msg, orig_msg)
        else:
            ai_messages = [message for message in original_messages if isinstance(message, AIMessage)]
            assistant_payloads = [message for message in payload_messages if message.get("role") == "assistant"]
            for payload_msg, ai_msg in zip(assistant_payloads, ai_messages):
                _restore_reasoning_content(payload_msg, ai_msg)

        return payload

    def _debug_stream_delta(self, delta: Mapping[str, Any], reasoning_text: str | None) -> None:
        count = getattr(self, "_deepseek_debug_chunk_count", 0)
        if count >= 12:
            return
        setattr(self, "_deepseek_debug_chunk_count", count + 1)
        logger.debug(
            "[ReasoningCompatibleChatOpenAI] delta[%d]: keys=%s content_len=%d reasoning_len=%d",
            count,
            sorted(delta.keys()),
            len(str(delta.get("content") or "")),
            len(reasoning_text or ""),
        )

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        if chunk.get("type") == "content.delta":
            return None

        token_usage = chunk.get("usage")
        choices = chunk.get("choices", []) or chunk.get("chunk", {}).get("choices", [])
        usage_metadata = _create_usage_metadata(token_usage, chunk.get("service_tier")) if token_usage else None

        if len(choices) == 0:
            generation_chunk = ChatGenerationChunk(
                message=default_chunk_class(content="", usage_metadata=usage_metadata),
                generation_info=base_generation_info,
            )
            # 与上游 1.3.4 对齐:保持 content 为 ""、不再置 []——空列表会让
            # merge_content 静默吞掉后续字符串正文,并触发 content_blocks 的
            # isinstance(list) 短路丢失 tool_call_chunks(R4-LM-10)。
            if self.output_version == "v1":
                generation_chunk.message.response_metadata["output_version"] = "v1"
            return generation_chunk

        choice = choices[0]
        delta = choice.get("delta")
        if delta is None:
            return None

        message_chunk = _convert_delta_to_message_chunk(delta, default_chunk_class)
        reasoning_text, raw_reasoning = _extract_reasoning_payload(delta)
        if isinstance(delta, Mapping):
            self._debug_stream_delta(delta, reasoning_text)

        generation_info = {**base_generation_info} if base_generation_info else {}
        if finish_reason := choice.get("finish_reason"):
            generation_info["finish_reason"] = finish_reason
            if model_name := chunk.get("model"):
                generation_info["model_name"] = model_name
            if system_fingerprint := chunk.get("system_fingerprint"):
                generation_info["system_fingerprint"] = system_fingerprint
            if service_tier := chunk.get("service_tier"):
                generation_info["service_tier"] = service_tier

        logprobs = choice.get("logprobs")
        if logprobs:
            generation_info["logprobs"] = logprobs

        if isinstance(message_chunk, AIMessageChunk):
            if usage_metadata:
                message_chunk.usage_metadata = usage_metadata
            if reasoning_text or raw_reasoning:
                message_chunk = _with_reasoning_content(
                    message_chunk,
                    reasoning_text,
                    raw_reasoning,
                )

        message_chunk.response_metadata["model_provider"] = "openai"
        return ChatGenerationChunk(
            message=message_chunk,
            generation_info=generation_info or None,
        )

    def _create_chat_result(
        self,
        response: dict | Any,
        generation_info: dict | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        response_dict = response if isinstance(response, dict) else response.model_dump()
        choices = response_dict.get("choices", [])

        generations: list[ChatGeneration] = []
        for index, generation in enumerate(result.generations):
            choice = choices[index] if index < len(choices) else {}
            message = generation.message
            if isinstance(message, AIMessage):
                choice_message = choice.get("message", {}) if isinstance(choice, Mapping) else {}
                reasoning_text, raw_reasoning = _extract_reasoning_payload(choice_message)
                if reasoning_text or raw_reasoning:
                    message = _with_reasoning_content(message, reasoning_text, raw_reasoning)
                    generation = ChatGeneration(
                        message=message,
                        generation_info=generation.generation_info,
                    )
            generations.append(generation)

        return ChatResult(generations=generations, llm_output=result.llm_output)
