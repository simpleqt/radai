"""工具结果状态提升与 checkpoint 前捕获实现(首参 self 为中间件实例)。"""

from __future__ import annotations

import logging

import asyncio
import json

from backend.core.agents.context import (
    CapabilityLedger,
    DEFAULT_PROJECT_EVIDENCE_TOOLS,
    DEFAULT_SEMANTIC_SUMMARY_SKIP_TOOLS,
)
from backend.core.agents.context.activation import apply_activation_to_objects
from backend.core.agents.context.ledger import context_item_to_record
from backend.core.agents.context.structured_result import compact_structured_result
from langchain.agents import AgentState
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command


logger = logging.getLogger(__name__)

# 工具消息投影的最低配额:低于此值按池耗尽处理(headline 级保底 + 降级横幅)。
_TOOL_MESSAGE_MIN_BUDGET_TOKENS = 512


PROJECT_EVIDENCE_TOOLS = set(DEFAULT_PROJECT_EVIDENCE_TOOLS)


RESULT_SUMMARY_SKIP_TOOLS = set(DEFAULT_SEMANTIC_SUMMARY_SKIP_TOOLS)


def _apply_context_object_path(
    self,
    captured,
    *,
    tool_name: str,
    content: str,
    tool_args: dict,
    thread_id: str,
    messages: list,
    artifact: object | None = None,
) -> None:
    """Segment + persist one tool result as a ContextObject (M0/M2/M4)."""

    if tool_name in {"context_read", "read_context_artifact"}:
        return
    if (
        self._context_object_mode == "off"
        or self._object_store is None
        or self._segmenter_registry is None
    ):
        return
    if self._context_object_mode == "shadow":
        ledger = self._temporary_capability_ledger(messages)
    else:
        ledger = self._ensure_capability_ledger(messages)
    if ledger is None:
        return
    source = artifact if isinstance(artifact, dict) else content
    try:
        obj = self._segmenter_registry.segment(
            tool_name=tool_name,
            result=source,
            tool_args=dict(tool_args or {}),
            thread_id=thread_id,
            turn_id=ledger.turn_id or None,
            user_id=self._user_id,
        )
    except Exception:
        logger.exception(
            "[ContextObject] segmenter failed; falling back to legacy externalization: tool=%s",
            tool_name,
        )
        obj = None
    if obj is None:
        if self._context_object_mode == "on":
            self._fallback_legacy_externalize(
                captured,
                tool_name=tool_name,
                content=content,
                thread_id=thread_id,
            )
        else:
            logger.warning(
                "[ContextObject] segmenter returned no object; tool=%s",
                tool_name,
            )
        return
    if not (obj.get("segments") or []):
        # 空段对象(错误/熔断/无结果载荷):没有任何可寻址正文,投影摘要仍会
        # 渲染"共 0 段证据"的非空文本并覆盖原文——web_search 的错误与
        # 反循环指令随之丢失,模型收不到停止信号会继续重试。保留处理器
        # 设置的原文替换,不入库、不投影。
        logger.info(
            "[ContextObject] segmenter produced no segments; keeping the raw "
            "tool message: tool=%s",
            tool_name,
        )
        return
    try:
        oid = self._object_store.put(obj)
    except Exception:
        # 存储层(MinIO/磁盘)异常不应把"捕获降级"升级为整个工具节点失败:
        # 工具本身已成功,回退旧 artifact 外置保住原文与证据记录。
        logger.exception(
            "[ContextObject] object store put failed; falling back to legacy "
            "externalization: tool=%s",
            tool_name,
        )
        if self._context_object_mode == "on":
            self._fallback_legacy_externalize(
                captured,
                tool_name=tool_name,
                content=content,
                thread_id=thread_id,
            )
        return
    self._fresh_context_oids.add(oid)
    obj["oid"] = oid
    captured.context_objects.append(
        {
            key: obj.get(key)
            for key in ("oid", "schema", "origin", "index")
            if obj.get(key) is not None
        }
    )
    for record in captured.context_records:
        if not str(record.get("object_ref") or "").strip():
            record["object_ref"] = oid
    if self._context_object_mode == "on" and self._projection_compiler is not None:
        try:
            query = str(
                (tool_args or {}).get("query")
                or self._latest_user_query(messages)
                or ""
            )
            activated = apply_activation_to_objects(
                [obj],
                current_query=query,
                messages=messages or [],
                active_turn_id=(
                    ledger.turn_id
                    or self._context_ledger._active_turn_id(list(messages or []))
                ),
                fresh_oids=self._fresh_context_oids,
                policy=self._policy,
                # Share the request-scoped term cache with the JIT path; sids are
                # content-addressed so entries stay valid, and without it every
                # tool capture re-tokenized its segments from scratch.
                term_cache=getattr(self, "_activation_term_cache", None),
            )
            project_obj = activated[0] if activated else obj
            if self._policy.tool_msg_budget_use_remaining:
                tool_message_budget = min(
                    max(0, ledger.remaining_tokens),
                    24000,
                )
            else:
                tool_message_budget = min(
                    self._policy.context_navigator_token_pool,
                    24000,
                )
            # 池将尽时强制 headline 级保底:预算为 0 的投影渲染空串,但横幅仍
            # 宣称\u201c已注入\u201d,模型会把工具结果当作无产出。保底让新证据至少以
            # 行/标题可寻址;有界超支由池计数的饱和语义兜住(remaining 归 0)。
            pool_exhausted = tool_message_budget < _TOOL_MESSAGE_MIN_BUDGET_TOKENS
            if pool_exhausted:
                tool_message_budget = _TOOL_MESSAGE_MIN_BUDGET_TOKENS
            result = ledger.project(
                [project_obj],
                budget_tokens=tool_message_budget,
                query=query,
                reason="tool_message",
            )
            if result is not None:
                if (result.rendered or "").strip():
                    banner = "[Context Object: 工具结果已按投影编译注入。]"
                    if pool_exhausted:
                        banner = (
                            "[Context Object: 共享投影池将尽,本结果以最低配额降级"
                            "注入;完整正文可用 context_read 按台账 sid 读取。]"
                        )
                    captured.replacement = banner + "\n" + result.rendered
                else:
                    # 连保底也渲染不出正文:如实声明,而不是宣称已注入。
                    captured.replacement = (
                        "[Context Object: 共享投影池已耗尽,本工具结果未能注入正文;"
                        "证据已登记,可用 context_read 按台账 sid 读取。]"
                    )
                captured.projection_receipts = list(ledger.receipts)
        except Exception:
            # 对象已入库、refs 已登记,后续 JIT 仍可投影;投影/激活异常只
            # 保留原文替换,不把工具节点炸掉。
            logger.exception(
                "[ContextObject] tool-message projection failed; keeping the "
                "raw tool message: tool=%s",
                tool_name,
            )
    self._record_context_accounting(ledger, captured, tool_name)


def _fallback_legacy_externalize(
    self,
    captured,
    *,
    tool_name: str,
    content: str,
    thread_id: str,
) -> None:
    """Persist a failed segmentation through the legacy artifact sink."""

    item = self._result_sink.ingest(
        source_tool=tool_name,
        raw_content=content,
        thread_id=thread_id,
        importance_hint=0.8,
        force_persist=True,
    )
    raw_ref = str(item.get("raw_ref") or "").strip() or None
    for record in captured.context_records:
        if not str(record.get("raw_ref") or "").strip() and raw_ref:
            record["raw_ref"] = raw_ref
    if not captured.context_records and item.get("id"):
        captured.context_records.append(context_item_to_record(item))
    captured.replacement = (
        "[Context Object: 分段失败，已回退旧 artifact 外置。]\n"
        + str(item.get("summary") or "")
    )


def _record_context_accounting(
    self,
    ledger: CapabilityLedger,
    captured,
    tool_name: str,
) -> None:
    accounting = ledger.accounting()
    accounting["tool"] = tool_name
    accounting["segmented_objects"] = len(captured.context_objects)
    logger.info(
        "[ContextAccounting] %s",
        json.dumps(accounting, ensure_ascii=False, default=str),
    )


async def _summarize_large_results(self, state: AgentState) -> dict[str, str]:
    messages = list(state.get("messages") or [])
    user_query = self._latest_user_query(messages)
    candidates: list[tuple[str, str, str]] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        content = str(message.content or "")
        tool_name = str(message.name or "unknown")
        if (
            len(content) <= self.char_threshold
            or tool_name in self._context_engine.semantic_summary_skip_tools
            or getattr(message, "status", None) == "error"
            or content.startswith(
                (
                    "[State Elevation:",
                    "[Context Isolation:",
                    "[Result Capture:",
                    "[Result Summarizer Handoff:",
                    "[Structured Result Compaction:",
                    "[Context Artifact Read:",
                    "[Context Object:",
                    "[System Notification:",
                )
            )
        ):
            continue
        candidates.append((str(message.tool_call_id or ""), tool_name, content))
        if len(candidates) >= self._policy.result_summary_max_concurrent:
            break
    if not candidates:
        return {}

    async def _run(tool_call_id: str, tool_name: str, content: str):
        summary = compact_structured_result(
            content,
            max_chars=self._policy.result_summary_output_chars,
        )
        deterministic = summary is not None
        if summary is None:
            summary = await asyncio.wait_for(
                self._large_result_summarizer.summarize(
                    source_tool=tool_name,
                    content=content,
                    user_query=user_query,
                ),
                timeout=self._policy.result_summary_timeout_seconds,
            )
        return tool_call_id, tool_name, summary, deterministic

    results = await asyncio.gather(
        *[_run(*candidate) for candidate in candidates],
        return_exceptions=True,
    )
    summaries: dict[str, str] = {}
    for result in results:
        if isinstance(result, BaseException):
            logger.warning(
                "[ResultSummarizer] child summarizer failed: %s",
                result,
            )
            continue
        tool_call_id, tool_name, summary, deterministic = result
        if not summary:
            continue
        if deterministic:
            summaries[tool_call_id] = (
                f"[Structured Result Compaction: source_tool={tool_name}; "
                "the existing JSON rows were compacted deterministically and "
                "no model or tool was called.]\n"
                f"{summary}"
            )
        else:
            summaries[tool_call_id] = (
                f"[Result Summarizer Handoff: source_tool={tool_name}; "
                "the child agent summarized an already completed tool result and "
                "did not perform another query.]\n"
                f"{summary}"
            )
    return summaries


def _process_state_elevation(
    self,
    state: AgentState,
    *,
    summary_overrides: dict[str, str] | None = None,
) -> dict | None:
    """Build reducer-friendly state updates for elevated content and pruned messages."""
    messages = state.get("messages") or []
    if not messages:
        return None

    loaded_skills: dict[str, str] = {}
    project_bootstrap: dict | None = None
    context_records: list[dict] = []
    context_objects: list[dict] = []
    projection_receipts: list[dict] = []
    message_updates = []
    thread_id = self._thread_id or self._resolve_thread_id(state)

    # Build ID mapping from AI messages to find matching tool arguments
    ai_tool_calls_map = {}
    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                tc_id = tc.get("id")
                if tc_id:
                    ai_tool_calls_map[tc_id] = tc

    for msg in messages:
        if isinstance(msg, ToolMessage):
            content_str = str(msg.content or "")

            # Check if it has already been pruned / elevated
            is_already_processed = (
                content_str.startswith("[State Elevation:")
                or content_str.startswith("[Context Isolation:")
                or content_str.startswith("[Result Capture:")
                or content_str.startswith("[Result Summarizer Handoff:")
                or content_str.startswith("[Structured Result Compaction:")
                or content_str.startswith("[Context Artifact Read:")
                or content_str.startswith("[Context Object:")
                or content_str.startswith("[System Notification:")
            )

            if is_already_processed or getattr(msg, "status", None) == "error":
                if getattr(msg, "artifact", None) is not None:
                    message_updates.append(
                        msg.model_copy(update={"artifact": None})
                    )
                continue
            if str(msg.name or "") == "query_project_tables":
                try:
                    query_payload = json.loads(content_str)
                except (TypeError, json.JSONDecodeError):
                    query_payload = {}
                if (
                    isinstance(query_payload, dict)
                    and query_payload.get("counts_toward_query_budget") is False
                ):
                    message_updates.append(
                        msg.model_copy(
                            update={
                                "content": (
                                    "[System Notification: This schema lookup or rejected SQL "
                                    "was not project evidence and did not consume query budget.]"
                                ),
                                "artifact": None,
                            }
                        )
                    )
                    continue
            matching_tc = ai_tool_calls_map.get(msg.tool_call_id, {})
            args = matching_tc.get("args", {})
            if not isinstance(args, dict):
                args = {"skill_name": args}
            try:
                captured = self._capture_registry.capture(
                    tool_name=str(msg.name or "unknown"),
                    content=content_str,
                    tool_args=args,
                    thread_id=thread_id,
                    artifact=getattr(msg, "artifact", None),
                )
            except Exception:
                logger.exception(
                    "[ContextCapture] Failed to capture tool result: tool=%s",
                    msg.name,
                )
                continue
            if captured is None:
                continue
            self._apply_context_object_path(
                captured,
                tool_name=str(msg.name or "unknown"),
                content=content_str,
                tool_args=args,
                thread_id=thread_id,
                messages=messages,
                artifact=getattr(msg, "artifact", None),
            )
            active_turn_id = self._context_ledger._active_turn_id(list(messages))
            retrieval_seq = next(
                (
                    index
                    for index, candidate in enumerate(messages)
                    if candidate is msg
                ),
                0,
            )
            for record in captured.context_records:
                if not str(record.get("turn_id") or "").strip():
                    record["turn_id"] = active_turn_id or None
                if str(record.get("category") or "") == "retrieval":
                    if not int(record.get("retrieval_seq") or 0):
                        record["retrieval_seq"] = retrieval_seq
            semantic_summary = (summary_overrides or {}).get(
                str(msg.tool_call_id or "")
            )
            if semantic_summary:
                captured.replacement = semantic_summary
                for record in captured.context_records:
                    record["claim"] = semantic_summary
                    record["content"] = semantic_summary
                    is_subagent_summary = semantic_summary.startswith(
                        "[Result Summarizer Handoff:"
                    )
                    if is_subagent_summary:
                        record["category"] = "handoff"
                        record["kind"] = "subagent_result"
                    record["tags"] = list(
                        dict.fromkeys(
                            [
                                *(record.get("tags") or []),
                                (
                                    "subagent_summary"
                                    if is_subagent_summary
                                    else "structured_result_compaction"
                                ),
                            ]
                        )
                    )
            loaded_skills.update(captured.loaded_skills)
            if captured.project_bootstrap:
                project_bootstrap = captured.project_bootstrap
            context_records.extend(captured.context_records)
            context_objects.extend(captured.context_objects)
            projection_receipts.extend(captured.projection_receipts)
            message_updates.append(
                msg.model_copy(
                    update={"content": captured.replacement, "artifact": None}
                )
            )
            logger.info(
                "[ContextCapture] Captured tool result: tool=%s records=%d",
                msg.name,
                len(captured.context_records),
            )

    updates: dict = {}
    if message_updates:
        updates["messages"] = message_updates
    if loaded_skills:
        updates["loaded_skills"] = loaded_skills
    if project_bootstrap:
        updates["project_bootstrap"] = project_bootstrap
    if context_records:
        updates["context_records"] = context_records
    if context_objects:
        updates["context_objects"] = context_objects
    if projection_receipts:
        updates["projection_receipts"] = projection_receipts
    return updates or None


def _capture_tool_message_before_checkpoint(
    self,
    request: ToolCallRequest,
    message: ToolMessage,
) -> tuple[ToolMessage, dict]:
    """Externalize one tool result before ToolNode writes graph state."""

    content = str(message.content or "")
    if not content or getattr(message, "status", None) == "error":
        if getattr(message, "artifact", None) is not None:
            return message.model_copy(update={"artifact": None}), {}
        return message, {}
    if self._is_captured_tool_content(content):
        if getattr(message, "artifact", None) is not None:
            return message.model_copy(update={"artifact": None}), {}
        return message, {}
    tool_name = str(message.name or request.tool_call.get("name") or "unknown")
    if tool_name == "query_project_tables":
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if (
            isinstance(payload, dict)
            and payload.get("counts_toward_query_budget") is False
        ):
            replacement = message.model_copy(
                update={
                    "content": (
                        "[System Notification: This schema lookup or rejected SQL "
                        "was not project evidence and did not consume query budget.]"
                    ),
                    "artifact": None,
                }
            )
            return replacement, {}

    state = request.state if isinstance(request.state, dict) else {}
    messages = list(state.get("messages") or [])
    thread_id = self._thread_id or self._resolve_thread_id(state)
    tool_args = request.tool_call.get("args") or {}
    if not isinstance(tool_args, dict):
        tool_args = {"value": tool_args}
    try:
        captured = self._capture_registry.capture(
            tool_name=tool_name,
            content=content,
            tool_args=tool_args,
            thread_id=thread_id,
            artifact=getattr(message, "artifact", None),
        )
    except Exception:
        logger.exception(
            "[ContextCapture] Failed before checkpoint: tool=%s",
            tool_name,
        )
        return message, {}
    if captured is None:
        return message, {}
    self._apply_context_object_path(
        captured,
        tool_name=tool_name,
        content=content,
        tool_args=tool_args,
        thread_id=thread_id,
        messages=messages,
        artifact=getattr(message, "artifact", None),
    )

    turn_id = self._context_ledger._active_turn_id(messages)
    retrieval_seq = len(messages)
    for record in captured.context_records:
        if not str(record.get("turn_id") or "").strip():
            record["turn_id"] = turn_id or None
        if str(record.get("category") or "") == "retrieval":
            if not int(record.get("retrieval_seq") or 0):
                record["retrieval_seq"] = retrieval_seq
    # The structured artifact is consumed by the capture adapter above. It
    # must not survive on the replacement ToolMessage because messages are
    # checkpointed by ToolNode before the next model request.
    replacement = message.model_copy(
        update={"content": captured.replacement, "artifact": None}
    )
    updates: dict = {}
    if captured.context_records:
        updates["context_records"] = list(captured.context_records)
    if captured.loaded_skills:
        updates["loaded_skills"] = dict(captured.loaded_skills)
    if captured.project_bootstrap:
        updates["project_bootstrap"] = dict(captured.project_bootstrap)
    if captured.context_objects:
        updates["context_objects"] = list(captured.context_objects)
    if captured.projection_receipts:
        updates["projection_receipts"] = list(captured.projection_receipts)
    return replacement, updates


def _capture_tool_result_before_checkpoint(
    self,
    request: ToolCallRequest,
    result: ToolMessage | Command,
) -> ToolMessage | Command:
    if isinstance(result, ToolMessage):
        replacement, updates = self._capture_tool_message_before_checkpoint(
            request,
            result,
        )
        if not updates and replacement is result:
            return result
        return Command(update={"messages": [replacement], **updates})
    if not isinstance(result, Command) or not isinstance(result.update, dict):
        return result

    original_update = dict(result.update)
    raw_messages = original_update.get("messages")
    if not isinstance(raw_messages, (list, tuple)):
        return result
    replacements: list = []
    accumulated: dict[str, object] = {}
    changed = False
    for candidate in raw_messages:
        if not isinstance(candidate, ToolMessage):
            replacements.append(candidate)
            continue
        replacement, updates = self._capture_tool_message_before_checkpoint(
            request,
            candidate,
        )
        replacements.append(replacement)
        changed = changed or replacement is not candidate or bool(updates)
        for key, value in updates.items():
            if isinstance(value, list):
                accumulated.setdefault(key, [])
                accumulated[key].extend(value)
            elif isinstance(value, dict):
                accumulated.setdefault(key, {})
                accumulated[key].update(value)
            else:
                accumulated[key] = value
    if not changed:
        return result
    original_update["messages"] = replacements
    for key, value in accumulated.items():
        existing = original_update.get(key)
        if isinstance(existing, list) and isinstance(value, list):
            original_update[key] = [*existing, *value]
        elif isinstance(existing, dict) and isinstance(value, dict):
            original_update[key] = {**existing, **value}
        else:
            original_update[key] = value
    return Command(
        graph=result.graph,
        update=original_update,
        resume=result.resume,
        goto=result.goto,
    )
