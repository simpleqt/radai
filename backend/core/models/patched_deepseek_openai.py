"""Patched ChatOpenAI adapter for DeepSeek reasoning over OpenAI-compatible gateways.

Some gateways expose DeepSeek models through an OpenAI-compatible streaming API,
but provider-specific reasoning fields are not preserved by the default
``langchain_openai.ChatOpenAI`` adapter. This provider keeps those fields on
streaming deltas and non-streaming responses, and mirrors ``reasoning_content``
back into assistant messages on subsequent turns when needed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import (
    _convert_delta_to_message_chunk,
    _create_usage_metadata,
)

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
        text = _best_effort_text(value).strip()
        if text and text not in texts:
            texts.append(text)

    reasoning_text = "\n\n".join(texts) if texts else None
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


class PatchedDeepSeekOpenAI(ChatOpenAI):
    """ChatOpenAI variant that preserves DeepSeek reasoning fields."""

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
        logger.info(
            "[PatchedDeepSeekOpenAI] delta[%d]: keys=%s content_len=%d reasoning_len=%d preview=%r reasoning_preview=%r",
            count,
            sorted(delta.keys()),
            len(str(delta.get("content") or "")),
            len(reasoning_text or ""),
            _best_effort_text(delta.get("content")).strip()[:120],
            (reasoning_text or "")[:120],
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
            if self.output_version == "v1":
                generation_chunk.message.content = []
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
