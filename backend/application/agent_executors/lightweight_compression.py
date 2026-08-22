"""轻量问答历史压缩/记忆:模块级缓存与压缩方法实现(首参 self 为 LightweightQAAgent)。"""

from __future__ import annotations

import logging
import asyncio
import os
import time

from backend.application.conversations import (
    build_memory_from_summary,
    format_memory_for_prompt,
    validate_memory_summary,
)
from backend.core.agents.context import HistoryCompactionResult
from backend.core.agents.middlewares.runtime_budget import (
    charge_agent_model_response,
    check_agent_runtime_budget,
)
from backend.core.models import create_chat_model_async
from collections import OrderedDict
from dataclasses import replace
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from typing import Any
from backend.application.agent_executors.lightweight_query_parsing import (
    _stringify_message_content,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 模块级压缩缓存：thread_id -> (摘要文本, 最后压缩消息 ID)
# 压缩在后台异步执行，结果下一轮生效
# ---------------------------------------------------------------------------
_COMPRESS_CACHE_MAX_ENTRIES = max(
    16, int(os.getenv("AGENT_MEMORY_CACHE_MAX_ENTRIES", "256") or "256")
)


_COMPRESSION_PERSIST_MAX_ATTEMPTS = max(
    1,
    int(os.getenv("AGENT_COMPRESSION_PERSIST_MAX_ATTEMPTS", "3") or "3"),
)


_COMPRESSION_PERSIST_TIMEOUT_SECONDS = max(
    1.0,
    float(os.getenv("AGENT_COMPRESSION_PERSIST_TIMEOUT_SECONDS", "10") or "10"),
)


_COMPRESSION_PERSIST_RETRY_DELAY_SECONDS = max(
    0.0,
    float(
        os.getenv("AGENT_COMPRESSION_PERSIST_RETRY_DELAY_SECONDS", "0.25")
        or "0.25"
    ),
)


_compress_cache: OrderedDict[str, tuple[str, str]] = OrderedDict()


_compress_tasks: dict[str, "asyncio.Task[None]"] = {}


_pending_compactions: dict[str, dict[str, Any]] = {}


def _get_compress_cache(thread_id: str) -> tuple[str, str] | None:
    value = _compress_cache.get(thread_id)
    if value is not None:
        _compress_cache.move_to_end(thread_id)
    return value


def _set_compress_cache(thread_id: str, value: tuple[str, str]) -> None:
    _compress_cache[thread_id] = value
    _compress_cache.move_to_end(thread_id)
    while len(_compress_cache) > _COMPRESS_CACHE_MAX_ENTRIES:
        _compress_cache.popitem(last=False)


def _get_thread_id(effective_config: RunnableConfig) -> str:
    configurable = (effective_config or {}).get("configurable") or {}
    return str(configurable.get("thread_id") or "default")


async def _persist_lightweight_compaction(
    self,
    effective_config: RunnableConfig,
    result: HistoryCompactionResult,
) -> bool:
    """Commit a compacted history through the graph/checkpoint boundary.

    Returns True only when the graph state was updated successfully.  A
    failed compaction is intentionally not treated as applied so callers
    can keep using the original history and avoid diverging from memory.
    """

    if self._graph is None or not hasattr(self._graph, "aupdate_state"):
        return False

    bound = None
    if self._compaction_checkpoints is not None:
        try:
            bound = self._compaction_checkpoints.bind(
                result,
                state={
                    "thread_data": {
                        "thread_id": self._lightweight_context_thread_id(
                            effective_config
                        )
                    }
                },
            )
        except Exception:
            logger.warning(
                "[LightweightQAAgent] compaction checkpoint binding failed",
                exc_info=True,
            )

    messages = list(bound.messages if bound is not None else result.messages)
    payload = dict(
        bound.state_payload if bound is not None else result.state_payload()
    )
    history_entry = {
        key: value for key, value in payload.items() if key != "summary"
    }
    if payload.get("summary"):
        history_entry["summary_preview"] = str(payload["summary"])[:1000]
    update: dict[str, Any] = {
        "messages": messages,
        "context_window_id": result.window_id,
        "context_compaction": payload,
        "context_compaction_history": [history_entry],
    }
    if bound is not None and bound.reference is not None:
        update["context_checkpoint_ref"] = bound.reference.as_dict()

    last_error: Exception | None = None
    for attempt in range(1, _COMPRESSION_PERSIST_MAX_ATTEMPTS + 1):
        try:
            await asyncio.wait_for(
                self._graph.aupdate_state(
                    effective_config,
                    update,
                    as_node=self._node_name,
                ),
                timeout=_COMPRESSION_PERSIST_TIMEOUT_SECONDS,
            )
            return True
        except Exception as exc:
            last_error = exc
            logger.warning(
                "[LightweightQAAgent] context compaction checkpoint "
                "persistence attempt %d/%d failed: %s",
                attempt,
                _COMPRESSION_PERSIST_MAX_ATTEMPTS,
                exc,
            )
            if attempt < _COMPRESSION_PERSIST_MAX_ATTEMPTS:
                await asyncio.sleep(
                    _COMPRESSION_PERSIST_RETRY_DELAY_SECONDS * attempt
                )
    logger.error(
        "[LightweightQAAgent] context compaction checkpoint persistence "
        "failed after %d attempts; graph history was left unchanged: %s",
        _COMPRESSION_PERSIST_MAX_ATTEMPTS,
        last_error,
    )
    return False


def _extend_pending_result_messages(
    self,
    result: HistoryCompactionResult,
    current_messages: list[Any],
) -> list[Any] | None:
    model_messages = result.model_messages
    if not model_messages:
        return None
    tail_id = self._message_id(model_messages[-1])
    if not tail_id:
        logger.warning(
            "[LightweightQAAgent] pending compaction has no tail id: "
            "model_message_ids=%s",
            [self._message_id(message) for message in model_messages],
        )
        return None
    found = False
    suffix: list[Any] = []
    for index in range(len(current_messages) - 1, -1, -1):
        if self._message_id(current_messages[index]) == tail_id:
            suffix = list(current_messages[index + 1 :])
            found = True
            break
    if not found:
        logger.warning(
            "[LightweightQAAgent] pending compaction tail not found: "
            "tail_id=%s current_ids=%s",
            tail_id,
            [self._message_id(message) for message in current_messages],
        )
        return None
    replacement = list(result.messages)
    replacement.extend(suffix)
    return replacement


async def _queue_pending_compaction(
    self,
    effective_config: RunnableConfig,
    result: HistoryCompactionResult,
) -> bool:
    thread_id = _get_thread_id(effective_config)
    conversation_thread_id = self._conversation_thread_id(thread_id)
    serialized = self._serialize_compaction_result(result)
    _pending_compactions[thread_id] = serialized
    while len(_pending_compactions) > _COMPRESS_CACHE_MAX_ENTRIES:
        _pending_compactions.pop(next(iter(_pending_compactions)), None)
    memory = dict(
        await self._memory_store.load(
            user_id=self._user_id,
            thread_id=conversation_thread_id,
        )
        or {}
    )
    previous = memory.get("pending_context_compaction") or {}
    memory["pending_context_compaction"] = {
        "result": serialized,
        "queued_at": time.time(),
        "attempts": int(
            (previous.get("attempts") or 0)
            if isinstance(previous, dict)
            else 0
        )
        + 1,
    }
    saved = await self._save_memory_with_retry(
        conversation_thread_id=conversation_thread_id,
        memory=memory,
    )
    if not saved:
        logger.error(
            "[LightweightQAAgent] pending compaction durable queue failed; "
            "window survives only in process memory: thread_id=%s window=%s",
            thread_id,
            result.window_id,
        )
    else:
        logger.warning(
            "[LightweightQAAgent] compaction persistence failed; queued for "
            "next-request retry: thread_id=%s window=%s",
            thread_id,
            result.window_id,
        )
    return saved


async def _retry_pending_lightweight_compaction(
    self,
    effective_config: RunnableConfig,
) -> bool:
    thread_id = _get_thread_id(effective_config)
    conversation_thread_id = self._conversation_thread_id(thread_id)
    memory = await self._memory_store.load(
        user_id=self._user_id,
        thread_id=conversation_thread_id,
    )
    memory_pending = (memory or {}).get("pending_context_compaction")
    payload = (
        memory_pending
        if isinstance(memory_pending, dict)
        else _pending_compactions.get(thread_id)
    )
    if not payload:
        return False
    result_payload = (
        payload.get("result") if isinstance(payload, dict) else payload
    )
    result = self._deserialize_compaction_result(result_payload)
    if result is None:
        await self._clear_pending_compaction(
            thread_id,
            conversation_thread_id,
        )
        return False
    current_messages = await self._load_recent_messages(effective_config)
    replacement = self._extend_pending_result_messages(
        result,
        current_messages,
    )
    if replacement is None:
        logger.warning(
            "[LightweightQAAgent] pending compaction tail missing in graph; "
            "dropping pending: thread_id=%s window=%s",
            thread_id,
            result.window_id,
        )
        await self._clear_pending_compaction(
            thread_id,
            conversation_thread_id,
        )
        return False
    retry_result = replace(result, messages=replacement)
    persisted = await self._persist_lightweight_compaction(
        effective_config,
        retry_result,
    )
    if not persisted:
        logger.warning(
            "[LightweightQAAgent] pending compaction retry failed; will "
            "retry on next request: thread_id=%s window=%s",
            thread_id,
            result.window_id,
        )
        return False
    await self._clear_pending_compaction(
        thread_id,
        conversation_thread_id,
    )
    logger.info(
        "[LightweightQAAgent] pending compaction persisted on retry: "
        "thread_id=%s window=%s",
        thread_id,
        result.window_id,
    )
    return True


async def _compact_lightweight_history(
    self,
    *,
    history_messages: list[Any],
    effective_config: RunnableConfig,
    system_content: str,
    current_message: HumanMessage,
) -> tuple[list[Any], HistoryCompactionResult | None]:
    if not history_messages:
        return [], None

    request_overhead = {"workflow": "lightweight", "tools": []}
    fixed_request_tokens = self._context_ledger.count_base_tokens(
        messages=[current_message],
        system_content=system_content,
        request_overhead=request_overhead,
    )
    attempt = await self._history_compactor.acompact_attempt(
        history_messages,
        previous_window_id=self._previous_compaction_window_id(history_messages),
        trigger="request_budget",
        additional_tokens=fixed_request_tokens,
        threshold_tokens=self._context_ledger.history_token_budget,
    )
    result = attempt.result
    if result is None:
        if attempt.threshold_reached:
            logger.info(
                "[LightweightQAAgent] history compaction skipped: reason=%s, tokens=%d, threshold=%d",
                attempt.skip_reason or "no_token_savings",
                attempt.tokens_before,
                attempt.threshold_tokens,
            )
        return list(history_messages), None

    persisted = await self._persist_lightweight_compaction(
        effective_config,
        result,
    )
    if not persisted:
        queued = await self._queue_pending_compaction(
            effective_config,
            result,
        )
        if not queued:
            logger.error(
                "[LightweightQAAgent] compaction persistence failed and "
                "durable pending queue failed: thread_id=%s window=%s",
                _get_thread_id(effective_config),
                result.window_id,
            )
        return result.model_messages, result
    logger.info(
        "[LightweightQAAgent] history compacted: tokens=%d→%d, removed=%d, window=%s",
        result.tokens_before,
        result.tokens_after,
        result.removed_messages,
        result.window_id,
    )
    return result.model_messages, result


async def _load_and_compress_history(
    self,
    effective_config: RunnableConfig,
    *,
    messages: list[Any] | None = None,
    persistent_memory: dict[str, Any] | None = None,
) -> str:
    thread_id = _get_thread_id(effective_config)
    conversation_thread_id = self._conversation_thread_id(thread_id)
    all_messages = (
        messages
        if messages is not None
        else await self._load_recent_messages(effective_config)
    )
    if persistent_memory is None:
        persistent_memory = await self._memory_store.load(
            user_id=self._user_id,
            thread_id=conversation_thread_id,
        )
    persistent_text = format_memory_for_prompt(persistent_memory)

    cached = _get_compress_cache(thread_id)
    if persistent_text:
        watermark = str(
            (persistent_memory or {}).get("last_compressed_message_id") or ""
        )
        new_messages = self._messages_after_watermark(all_messages, watermark)
        new_text = self._format_messages(new_messages)
        history_text = (
            f"{persistent_text}\n\n[最近对话]\n{new_text}"
            if new_text
            else persistent_text
        )
    elif cached:
        summary, watermark = cached
        new_messages = self._messages_after_watermark(all_messages, watermark)
        new_text = self._format_messages(new_messages)
        history_text = (
            f"[历史摘要]\n{summary}\n\n[最近对话]\n{new_text}"
            if new_text
            else summary
        )
    else:
        history_text = self._format_messages(all_messages[-self._history_limit :])

    if not history_text:
        return "无"

    if len(history_text) > self._history_compress_chars:
        truncated = history_text[-self._history_compress_chars :]
        if "\n" in truncated:
            truncated = truncated[truncated.find("\n") + 1 :]
        history_text = (
            "（较早对话内容已因超长被截断，以下为最近节选）\n" + truncated
        )

    return history_text


async def _save_memory_with_retry(
    self,
    *,
    conversation_thread_id: str | None,
    memory: dict[str, Any],
) -> bool:
    saved = False
    for attempt in range(1, _COMPRESSION_PERSIST_MAX_ATTEMPTS + 1):
        saved = await self._memory_store.save(
            user_id=self._user_id,
            thread_id=conversation_thread_id,
            memory=memory,
        )
        if saved:
            break
        logger.warning(
            "[LightweightQAAgent] 摘要/待压缩持久化尝试 %d/%d 失败: thread_id=%s",
            attempt,
            _COMPRESSION_PERSIST_MAX_ATTEMPTS,
            conversation_thread_id,
        )
        if attempt < _COMPRESSION_PERSIST_MAX_ATTEMPTS:
            await asyncio.sleep(
                _COMPRESSION_PERSIST_RETRY_DELAY_SECONDS * attempt
            )
    if not saved:
        logger.error(
            "[LightweightQAAgent] 摘要/待压缩持久化失败 %d 次: thread_id=%s",
            _COMPRESSION_PERSIST_MAX_ATTEMPTS,
            conversation_thread_id,
        )
    return saved


async def _do_background_compression(
    self, effective_config: RunnableConfig, thread_id: str
) -> None:
    try:
        conversation_thread_id = self._conversation_thread_id(thread_id)
        all_messages = await self._load_recent_messages(effective_config)
        previous_memory = await self._memory_store.load(
            user_id=self._user_id,
            thread_id=conversation_thread_id,
        )
        watermark = str(
            (previous_memory or {}).get("last_compressed_message_id") or ""
        )
        new_messages = self._messages_after_watermark(all_messages, watermark)
        if previous_memory and not new_messages:
            return
        new_text = self._format_messages(
            new_messages if previous_memory else all_messages
        )
        previous_text = format_memory_for_prompt(previous_memory)
        history_text = (
            f"{previous_text}\n\n[最近对话]\n{new_text}"
            if previous_text and new_text
            else previous_text or new_text
        )

        if not history_text or len(history_text) <= self._history_compress_chars:
            return

        msg_count = len(all_messages)
        # Watermark must be the last message actually covered by this
        # summary snapshot.  New turns appended while the model is running
        # must remain visible in the next prompt instead of being silently
        # skipped by an over-advanced watermark.
        compressed_message_id = (
            self._message_id(all_messages[-1]) if all_messages else ""
        )
        prompt = (
            "请将以下多轮对话压缩成面向后续问答的上下文摘要，保留：用户目标、关键事实、已给结论、重要数值、引用约束、未解决问题。"
            "不要添加新事实，控制在 1200 字以内。\n\n"
            f"{history_text}"
        )
        compression_model_name = self._model_name or "MiniMax-M2.7"
        summary: str | None = None

        try:
            model = await create_chat_model_async(
                name=compression_model_name, thinking_enabled=False
            )
            await check_agent_runtime_budget()
            response = await asyncio.wait_for(model.ainvoke(prompt), timeout=40.0)
            await charge_agent_model_response(response)
            summary = (
                _stringify_message_content(
                    getattr(response, "content", response)
                ).strip()
                or None
            )
        except Exception as exc:
            if compression_model_name != "MiniMax-M2.7":
                try:
                    logger.info(
                        "[LightweightQAAgent] 主模型后台压缩失败，尝试 MiniMax-M2.7 兜底: %s",
                        exc,
                    )
                    model = await create_chat_model_async(
                        name="MiniMax-M2.7", thinking_enabled=False
                    )
                    await check_agent_runtime_budget()
                    response = await asyncio.wait_for(
                        model.ainvoke(prompt), timeout=30.0
                    )
                    await charge_agent_model_response(response)
                    summary = (
                        _stringify_message_content(
                            getattr(response, "content", response)
                        ).strip()
                        or None
                    )
                except Exception:
                    logger.warning(
                        "[LightweightQAAgent] MiniMax 历史压缩回退失败: "
                        "thread_id=%s",
                        thread_id,
                        exc_info=True,
                    )
            else:
                logger.warning(
                    "[LightweightQAAgent] 后台历史压缩失败: thread_id=%s, %s",
                    thread_id,
                    exc,
                )

        if summary:
            if not validate_memory_summary(
                source_text=history_text, summary=summary
            ):
                logger.warning(
                    "[LightweightQAAgent] 后台摘要未通过事实约束校验，跳过持久化: thread_id=%s",
                    thread_id,
                )
                return
            memory = build_memory_from_summary(
                summary=summary,
                message_count=msg_count,
                message_id=compressed_message_id,
                previous=previous_memory,
            )
            saved = await self._save_memory_with_retry(
                conversation_thread_id=conversation_thread_id,
                memory=memory,
            )
            if not saved:
                logger.error(
                    "[LightweightQAAgent] 后台摘要持久化失败，未更新进程缓存: "
                    "thread_id=%s",
                    thread_id,
                )
                return
            _set_compress_cache(thread_id, (summary, compressed_message_id))
            logger.info(
                "[LightweightQAAgent] 后台压缩完成（下轮生效）: thread_id=%s, model=%s, "
                "raw_chars=%d, summary_chars=%d, compressed_msgs=%d, watermark=%s",
                thread_id,
                compression_model_name,
                len(history_text),
                len(summary),
                msg_count,
                compressed_message_id,
            )
    except Exception as exc:
        logger.warning(
            "[LightweightQAAgent] 后台压缩任务异常: thread_id=%s, %s",
            thread_id,
            exc,
        )
    finally:
        _compress_tasks.pop(thread_id, None)
