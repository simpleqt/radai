"""轻量问答主流式管线 aquery_stream 实现(首参 self 为 LightweightQAAgent)。"""

from __future__ import annotations

import logging
import asyncio
import sys
import time
from contextlib import suppress

from backend.application.agent_executors.stream_utils import extract_web_search_metadata
from backend.application.conversations import format_memory_for_prompt
from backend.application.retrieval.query_resolution import latest_user_message_text
from backend.config.log_privacy import text_fingerprint
from backend.core.agents.middlewares.runtime_budget import (
    charge_agent_model_response,
    check_agent_runtime_budget,
)
from backend.core.models import create_chat_model_async
from backend.core.utils.async_tasks import (
    reap_task_bounded,
    task_is_cancelling,
)
from langchain_core.runnables import RunnableConfig
from typing import Any
from backend.application.agent_executors.lightweight_compression import (
    _get_thread_id,
)
from backend.application.agent_executors.lightweight_query_parsing import (
    _clean_question,
    _display_search_detail,
    _extract_file_search_terms,
    _extract_reasoning_content,
    _is_table_analysis_query,
    _kb_result_warning,
    _make_process_event,
    _stringify_message_content,
    _summarize_kb_for_process_ui,
    _summarize_text,
)


logger = logging.getLogger(__name__)

# 有界回收生产者任务的上限：取消无法立刻生效时也不无限等待，避免整个请求挂死。
_STREAM_PRODUCER_CLEANUP_TIMEOUT_SECONDS = 5.0
# 断连/超时兜底保存的时限:teardown 期间绝不无限等待 checkpoint 写入。
_LIGHTWEIGHT_PARTIAL_SAVE_TIMEOUT_SECONDS = 5.0


# ------------------------------------------------------------------
# 核心流式查询：直接检索 → 流式回答，无模型决策环节
# ------------------------------------------------------------------
async def aquery_stream(
    self,
    *,
    question: str,
    effective_config: RunnableConfig,
    stream_started_at: float | None = None,
    image_contents: list[dict] | None = None,
):
    stream_started_at = stream_started_at or time.monotonic()

    yield {"type": "status", "content": "已接收问题，正在分析..."}

    phase_started_at = time.monotonic()

    # 1) 加载历史上下文
    conversation_thread_id = self._conversation_thread_id(
        _get_thread_id(effective_config)
    )
    # If a previous compaction could not reach the graph checkpointer,
    # retry the exact pending window now (including any turns appended
    # since) before taking a fresh history snapshot.
    # 它内部自行读取 memory 判定 pending 状态;此处不预读一份立即被
    # 覆盖的副本(每请求省一次持久层读取)。
    await self._retry_pending_lightweight_compaction(effective_config)
    persistent_memory = await self._memory_store.load(
        user_id=self._user_id,
        thread_id=conversation_thread_id,
    )
    # Read memory before messages: a background compaction that finishes
    # between the two reads must never be paired with a stale message
    # snapshot, otherwise the watermark lookup can silently re-include
    # already-compressed turns.
    history_messages = await self._load_recent_messages(effective_config)
    normalized_history_messages = self._normalize_history_messages(history_messages)
    persistent_memory_text = format_memory_for_prompt(persistent_memory)
    history_text = await self._load_and_compress_history(
        effective_config,
        messages=history_messages,
        # An empty mapping is intentional: it prevents the compatibility
        # loader from issuing a second store read when no memory exists.
        persistent_memory=persistent_memory or {},
    )
    logger.info(
        "[LightweightQAAgent] 阶段耗时: history_load=%dms",
        int((time.monotonic() - phase_started_at) * 1000),
    )
    clean_q = _clean_question(question)

    # 2) 检查是否有会话级上传文档索引（提前检查，影响搜索词生成策略）
    file_index_context = ""
    table_analysis_context = ""
    has_file_index = False
    file_idx_ref = None
    table_files: list[dict[str, Any]] = []
    file_context_thread_id = self._uploaded_files_thread_id or self._thread_id
    raw_file_ids = self._request_metadata.get("file_ids")
    if isinstance(raw_file_ids, (list, tuple, set)):
        explicit_file_ids = [
            str(item).strip() for item in raw_file_ids if str(item or "").strip()
        ]
    else:
        explicit = str(raw_file_ids or "").strip()
        explicit_file_ids = [explicit] if explicit else []
    if file_context_thread_id:
        try:
            from backend.services.chat_file_index import get_index_manager

            if self._user_id:
                file_idx_ref = await get_index_manager().get_async(
                    file_context_thread_id,
                    user_id=self._user_id,
                )
            if file_idx_ref and file_idx_ref.chunk_count > 0:
                has_file_index = True
        except Exception:
            logger.warning(
                "[LightweightQAAgent] 获取会话文件索引失败: thread_id=%s",
                file_context_thread_id,
                exc_info=True,
            )
    if file_context_thread_id or explicit_file_ids:
        try:
            from backend.services.table_analysis_files import (
                list_table_files_by_ids,
                list_table_files_by_thread,
            )

            # 同步 SQLAlchemy 查询必须离开事件循环:Agent API 单 worker,
            # MySQL 慢查询/同步池排队期间所有会话的 SSE 流都会冻结。
            if file_context_thread_id:
                table_files = await asyncio.to_thread(
                    list_table_files_by_thread,
                    file_context_thread_id,
                    user_id=self._user_id,
                )
            if explicit_file_ids:
                by_id = await asyncio.to_thread(
                    list_table_files_by_ids,
                    explicit_file_ids,
                    user_id=self._user_id,
                )
                seen_ids = {str(item.get("file_id") or "") for item in table_files}
                table_files.extend(
                    item
                    for item in by_id
                    if str(item.get("file_id") or "") not in seen_ids
                )
        except Exception as exc:
            logger.warning("[LightweightQAAgent] 表格文件清单读取失败: %s", exc)

    # 3) 生成 KB/Web 检索词
    # - 仅知识库、首轮无历史/无上传文档：直接用用户原句（KB 内部有关键词提取）
    # - 启用联网搜索，或有历史/上传文档：由模型提炼检索词
    # - 优化: 探索式学习模式直接使用知识点标题,跳过LLM提取
    has_retrieval = bool(self._kb_ids or self._use_search)
    has_history = bool(history_text and history_text != "无")
    skip_kb = False
    table_intent = _is_table_analysis_query(clean_q, table_files)

    if self._use_direct_query:
        # 探索式学习: 直接使用知识点标题,跳过LLM提取检索词
        search_query = clean_q
        logger.info(
            "[LightweightQAAgent] 探索式学习直查模式: query_meta=%s",
            text_fingerprint(search_query),
        )
    else:
        needs_model_query = self._use_search or has_history or has_file_index
        if has_retrieval and needs_model_query:
            phase_started_at = time.monotonic()
            search_query, skip_kb = await self._extract_search_query_v2(
                clean_q,
                history_text,
                has_file_index=has_file_index,
                file_idx=file_idx_ref,
                previous_user_question=latest_user_message_text(
                    normalized_history_messages
                ),
            )
            logger.info(
                "[LightweightQAAgent] 阶段耗时: search_query=%dms, skip_kb=%s, query_meta=%s",
                int((time.monotonic() - phase_started_at) * 1000),
                skip_kb,
                text_fingerprint(search_query),
            )
        elif has_retrieval:
            search_query = clean_q
            logger.info(
                "[LightweightQAAgent] 仅知识库首轮，旁路检索词 LLM 提取: query_meta=%s",
                text_fingerprint(search_query),
            )
        else:
            search_query = clean_q

    if table_intent:
        yield {"type": "status", "content": "轻量问答模式：正在分析上传表格..."}
        yield {
            "type": "event",
            "event": _make_process_event(
                kind="tool_start",
                title="表格分析开始",
                detail=f"已识别 {len(table_files)} 个可分析表格文件",
                tool="table_analysis",
            ),
        }
        phase_started_at = time.monotonic()
        analysis_task = asyncio.ensure_future(
            asyncio.wait_for(
                self._run_table_analysis(clean_q, table_files),
                timeout=float(self.TABLE_ANALYSIS_TIMEOUT_SECONDS),
            )
        )
        try:
            # 分析最长可达 TABLE_ANALYSIS_TIMEOUT_SECONDS,期间保持 SSE
            # 心跳,避免长时间零字节被网关/代理断连。
            while True:
                done_set, _ = await asyncio.wait(
                    {analysis_task},
                    timeout=float(self.STREAM_HEARTBEAT_INTERVAL_SECONDS),
                )
                if done_set:
                    break
                yield {"type": "ping", "timestamp": int(time.time() * 1000)}
            table_analysis_context = analysis_task.result()
            summary = _summarize_text(table_analysis_context, max_length=500)
            yield {
                "type": "event",
                "event": _make_process_event(
                    kind="tool_end",
                    title="表格分析完成",
                    detail="已获取表格结构化分析结果",
                    tool="table_analysis",
                    level="success",
                    duration_ms=int((time.monotonic() - phase_started_at) * 1000),
                    summary=summary,
                ),
            }
            if summary:
                yield {
                    "type": "event",
                    "event": _make_process_event(
                        kind="tool_result",
                        title="表格分析摘要",
                        detail=summary,
                        tool="table_analysis",
                        metadata={"summary": summary},
                    ),
                }
            skip_kb = True
        except Exception as exc:
            logger.warning("[LightweightQAAgent] 表格分析失败: %s", exc)
            yield {
                "type": "event",
                "event": _make_process_event(
                    kind="error",
                    title="表格分析失败",
                    detail=str(exc),
                    tool="table_analysis",
                    level="error",
                ),
            }
        finally:
            # 断连(GeneratorExit)在 ping yield 处解栈时,分析任务必须被取消
            # 并有界回收,不能留下孤儿任务继续读取表格。
            await reap_task_bounded(analysis_task)

    # 4) 生成上传文档检索词（避免直接用用户原句作为检索词）
    file_search_query = clean_q
    file_keywords: list[str] = []
    if has_file_index and file_idx_ref:
        from backend.services.chat_file_index import _is_generic_summary_query

        if _is_generic_summary_query(clean_q):
            # 通用总结/概述类查询：直接旁路 LLM，使用文档结构关键词实现极速秒级响应，检索词优雅展示
            file_search_query = "abstract introduction summary main contribution"
            file_keywords = []
            logger.info(
                "[LightweightQAAgent] 通用总结/概述型问题，直接旁路文档检索词 LLM 提取"
            )
        else:
            yield {
                "type": "status",
                "content": "轻量问答模式：正在分析文档检索需求...",
            }
            phase_started_at = time.monotonic()
            file_search_query, file_keywords = await _extract_file_search_terms(
                clean_q,
                file_idx_ref,
                self._model_name,
                prompt_profile=self._prompt_profile,
                domain=self._prompt_domain,
                prompt_registry=self._prompt_registry,
                file_search_prefix=self._lightweight_file_search_prefix,
            )
            logger.info(
                "[LightweightQAAgent] 阶段耗时: file_search_terms=%dms, query_meta=%s",
                int((time.monotonic() - phase_started_at) * 1000),
                text_fingerprint(file_search_query),
            )

    # 5) 并发执行检索（KB / Web / 上传文档三路并行，skip_kb 时跳过前两路）
    kb_context = ""
    web_context = ""

    async def _do_kb_search() -> str:
        return await self._run_kb_search(search_query)

    async def _do_web_search() -> str:
        return await self._run_web_search(search_query)

    async def _do_file_search() -> str:
        """直接检索会话级文档索引（内部自动做多轮召回与合并）。"""
        try:
            if "chat_file_search" not in self._runtime_tools:
                return ""
            result = await self._invoke_runtime_tool(
                "chat_file_search",
                {
                    "query": file_search_query,
                    "keywords": file_keywords,
                    "top_k": 7,
                },
            )
            return str(result or "")
        except Exception as e:
            logger.warning("[LightweightQAAgent] 文件索引检索失败: %s", e)
            return ""

    # 保存工厂、gather 时才创建协程:此前协程在多个状态 yield 点之前就已
    # 创建,消费方在这些 yield 处断连(GeneratorExit)会留下从未 await 的
    # 孤儿协程。
    search_specs: list[tuple[str, Any]] = []
    if self._kb_ids and not skip_kb:
        search_specs.append(("kb", _do_kb_search))
    if self._use_search and not skip_kb:
        search_specs.append(("web", _do_web_search))
    if has_file_index:
        search_specs.append(("file", _do_file_search))

    if search_specs:
        status_parts = []
        if self._kb_ids and not skip_kb:
            status_parts.append("知识库")
        if self._use_search and not skip_kb:
            status_parts.append("联网")
        if has_file_index:
            status_parts.append("上传文档")
        if skip_kb and has_file_index:
            status_parts = ["上传文档（已跳过知识库）"]
        yield {
            "type": "status",
            "content": f"轻量问答模式：正在检索（{'、'.join(status_parts)}）...",
        }

        # 发出检索开始事件（skip_kb 时不展示 KB/联网步骤）
        display_query = _display_search_detail(
            search_query,
            skip_kb=skip_kb,
            fallback=clean_q,
        )
        if self._kb_ids and not skip_kb:
            yield {
                "type": "event",
                "event": _make_process_event(
                    kind="tool_start",
                    title="知识库检索开始",
                    detail=f"检索词: {display_query}",
                    tool="multi_kb_search",
                ),
            }
        if self._use_search and not skip_kb:
            yield {
                "type": "event",
                "event": _make_process_event(
                    kind="tool_start",
                    title="联网搜索开始",
                    detail=f"检索词: {display_query}",
                    tool="web_search",
                ),
            }
        if has_file_index:
            yield {
                "type": "event",
                "event": _make_process_event(
                    kind="tool_start",
                    title="上传文档检索开始",
                    detail=f"检索词: {file_search_query[:80]}",
                    tool="chat_file_search",
                ),
            }

        # 并发执行所有检索;期间保持 SSE 心跳——KB/联网检索的内部超时可达
        # 数十秒,长时间零字节输出会被网关/代理提前断连。
        phase_started_at = time.monotonic()
        retrieval_task = asyncio.ensure_future(
            asyncio.gather(
                *[factory() for _, factory in search_specs],
                return_exceptions=True,
            )
        )
        try:
            while True:
                done_set, _ = await asyncio.wait(
                    {retrieval_task},
                    timeout=float(self.STREAM_HEARTBEAT_INTERVAL_SECONDS),
                )
                if done_set:
                    break
                yield {"type": "ping", "timestamp": int(time.time() * 1000)}
            results = retrieval_task.result()
        finally:
            # 断连时取消整组检索并有界回收,不留孤儿任务烧检索预算。
            await reap_task_bounded(retrieval_task)

        for (label, _), result in zip(search_specs, results):
            elapsed_ms = int((time.monotonic() - phase_started_at) * 1000)
            if isinstance(result, BaseException):
                logger.warning(
                    "[LightweightQAAgent] 检索异常 (%s): %s", label, result
                )
                result = ""

            if label == "kb":
                raw_kb_context = str(result)
                kb_warning = _kb_result_warning(raw_kb_context)
                kb_context = "" if kb_warning else raw_kb_context
                summary = kb_warning or _summarize_kb_for_process_ui(
                    kb_context,
                    max_length=500,
                )
                yield {
                    "type": "event",
                    "event": _make_process_event(
                        kind="tool_end",
                        title=(
                            "知识库检索受限" if kb_warning else "知识库检索完成"
                        ),
                        detail=(
                            kb_warning
                            if kb_warning
                            else "已获取所选知识库的相关片段"
                        ),
                        tool="multi_kb_search",
                        level="warning" if kb_warning else "success",
                        duration_ms=elapsed_ms,
                        summary=summary,
                    ),
                }
                if summary:
                    yield {
                        "type": "event",
                        "event": _make_process_event(
                            kind="tool_result",
                            title=(
                                "知识库检索提示" if kb_warning else "知识库结果摘要"
                            ),
                            detail=summary,
                            tool="multi_kb_search",
                            level="warning" if kb_warning else "info",
                            metadata={
                                "summary": summary,
                                "evidence_injected": not bool(kb_warning),
                            },
                        ),
                    }
            elif label == "web":
                web_context = str(result)
                web_meta = extract_web_search_metadata(
                    result if not isinstance(result, BaseException) else web_context
                )
                total = web_meta.get("total_results")
                url_count = len(web_meta.get("urls") or [])
                summary = _summarize_text(web_context, max_length=500)
                yield {
                    "type": "event",
                    "event": _make_process_event(
                        kind="tool_end",
                        title="联网搜索完成",
                        detail=(
                            f"已获取 {total} 条网页结果（{url_count} 个来源链接）"
                            if isinstance(total, int)
                            else "已获取互联网搜索结果"
                        ),
                        tool="web_search",
                        level="success",
                        duration_ms=elapsed_ms,
                        summary=summary,
                        metadata=web_meta,
                    ),
                }
                if summary or web_meta:
                    yield {
                        "type": "event",
                        "event": _make_process_event(
                            kind="tool_result",
                            title="联网结果摘要",
                            detail=summary or f"共 {url_count} 个网页来源",
                            tool="web_search",
                            metadata={**web_meta, "summary": summary},
                        ),
                    }
            elif label == "file":
                file_index_context = str(result)
                summary = _summarize_text(file_index_context, max_length=300)
                yield {
                    "type": "event",
                    "event": _make_process_event(
                        kind="tool_end",
                        title="上传文档检索完成",
                        detail=f"从上传文档中找到 {len(file_index_context.split(chr(10)) if file_index_context else [])} 行相关内容",
                        tool="chat_file_search",
                        level="success",
                        duration_ms=elapsed_ms,
                        summary=summary,
                    ),
                }
                if summary:
                    yield {
                        "type": "event",
                        "event": _make_process_event(
                            kind="tool_result",
                            title="文档检索结果摘要",
                            detail=summary,
                            tool="chat_file_search",
                            metadata={"summary": summary},
                        ),
                    }

        logger.info(
            "[LightweightQAAgent] 阶段耗时: retrieval=%dms, kb_chars=%d, web_chars=%d, file_chars=%d",
            int((time.monotonic() - phase_started_at) * 1000),
            len(kb_context),
            len(web_context),
            len(file_index_context),
        )

    phase_started_at = time.monotonic()

    image_info = f", images={len(image_contents)}" if image_contents else ""
    image_size = (
        sum(len(img.get("base64", "")) for img in image_contents)
        if image_contents
        else 0
    )
    image_info += f", image_bytes={image_size}" if image_size > 0 else ""

    # 2.5) 构建长文档全局概要（常驻 System Prompt，帮助模型了解文档全局及辅助检索）
    permanent_file_prefix = ""
    if has_file_index and file_idx_ref:
        try:
            unique_files = []
            seen_files = set()
            for c in file_idx_ref._chunks:
                if c.file_id not in seen_files:
                    seen_files.add(c.file_id)
                    unique_files.append((c.file_id, c.file_name))

            from backend.config.settings import settings

            prefix_len = getattr(settings, "long_doc_prefix_length", 1000)

            prefix_parts = []
            for file_id, file_name in unique_files:
                chunks = [c for c in file_idx_ref._chunks if c.file_id == file_id]
                chunks.sort(key=lambda x: x.chunk_index)
                full_text = ""
                for c in chunks:
                    full_text += c.content + "\n"
                    if len(full_text) >= prefix_len:
                        break
                prefix_content = full_text[:prefix_len].strip()
                prefix_parts.append(
                    f"长文档名：{file_name}\n"
                    f"文档前 {prefix_len} 字内容概要（包含摘要/目录/引言等重要信息）：\n"
                    f'"""\n{prefix_content}\n"""'
                )
            if prefix_parts:
                permanent_file_prefix = (
                    "[已上传长文档全局概要信息]\n" + "\n\n".join(prefix_parts)
                )
        except Exception as e:
            logger.warning("[LightweightQAAgent] 构建长文档概要失败: %s", e)

    # 3) 通过统一 ContextPolicy/HistoryCompactor/ContextLedger 构建请求。
    # SystemMessage 仍保持唯一且位于首位；历史和检索结果不再直接拼进系统提示。
    messages, context_meta = await self._assemble_lightweight_context(
        clean_q=clean_q,
        effective_config=effective_config,
        history_messages=normalized_history_messages,
        persistent_memory_text=persistent_memory_text,
        kb_context=kb_context,
        web_context=web_context,
        file_index_context=file_index_context,
        table_analysis_context=table_analysis_context,
        permanent_file_prefix=permanent_file_prefix,
        image_contents=image_contents,
    )
    pack = context_meta.get("pack") or {}
    pack_budget = pack.get("budget") or {}
    logger.info(
        "[LightweightQAAgent] 阶段耗时: prompt_build=%dms, domain=%s, system_chars=%d, "
        "history_pruned=%s, context_tokens=%s, dynamic_tokens=%s, context_items=%s%s",
        int((time.monotonic() - phase_started_at) * 1000),
        self._prompt_domain,
        int(context_meta.get("system_chars") or 0),
        bool(context_meta.get("history_pruned")),
        pack_budget.get("tokens_used", context_meta.get("final_tokens", 0)),
        pack_budget.get("dynamic_tokens_used", 0),
        pack_budget.get("items_injected", 0),
        image_info,
    )

    # 4) 流式生成回答
    phase_started_at = time.monotonic()
    model = await create_chat_model_async(
        name=self._model_name, thinking_enabled=self._thinking_enabled
    )
    logger.info(
        "[LightweightQAAgent] 阶段耗时: model_init=%dms, model=%s",
        int((time.monotonic() - phase_started_at) * 1000),
        self._model_name,
    )
    yield {"type": "status", "content": "已开始生成回答..."}

    from backend.application.agent_executors.image_url_registry import (
        StreamingTokenResolver,
    )

    token_resolver = StreamingTokenResolver()

    answer_parts: list[str] = []
    turn_saved = False
    chunk_count = 0
    first_chunk_delay_ms: int | None = None
    first_chunk_from_answer_model_ms: int | None = None
    heartbeat_count = 0
    pre_content_chunk_debug_count = 0
    stream_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(
        maxsize=self.STREAM_QUEUE_MAXSIZE
    )

    async def _produce_model_chunks() -> None:
        try:
            chunk_idx = 0
            async for chunk in model.astream(messages):
                if chunk_idx == 0:
                    logger.info(
                        "[LightweightQAAgent] 主模型 astream 产生首个 chunk 对象"
                    )
                await stream_queue.put(("chunk", chunk))
                chunk_idx += 1
        except asyncio.CancelledError:
            # 取消时绝不再向队列写：满队列上的 put 会永久阻塞，而取消信号已被
            # 消费，任务再也不会被唤醒——将死锁并泄漏底层模型流式 HTTP 连接。
            raise
        except Exception as exc:
            if not task_is_cancelling(asyncio.current_task()):
                await stream_queue.put(("error", exc))
        finally:
            if not task_is_cancelling(asyncio.current_task()):
                await stream_queue.put(("done", None))

    # 与 stream_started_at（整段流水线起点）区分，便于判断慢在检索/改写还是主模型首包
    answer_model_stream_started_at = time.monotonic()
    last_model_event_at = answer_model_stream_started_at
    logger.info("[LightweightQAAgent] 开始调用主模型 astream...")
    await check_agent_runtime_budget()
    producer_task = asyncio.create_task(_produce_model_chunks())
    last_model_chunk = None
    try:
        stream_done = False
        while not stream_done:
            now = time.monotonic()
            total_elapsed = now - answer_model_stream_started_at
            idle_elapsed = now - last_model_event_at
            if total_elapsed >= self.STREAM_TOTAL_TIMEOUT_SECONDS:
                raise asyncio.TimeoutError(
                    f"轻量回答生成超过 {self.STREAM_TOTAL_TIMEOUT_SECONDS} 秒"
                )
            if idle_elapsed >= self.STREAM_IDLE_TIMEOUT_SECONDS:
                raise asyncio.TimeoutError(
                    f"轻量回答连续 {self.STREAM_IDLE_TIMEOUT_SECONDS} 秒无模型事件"
                )
            wait_timeout = min(
                float(self.STREAM_HEARTBEAT_INTERVAL_SECONDS),
                float(self.STREAM_TOTAL_TIMEOUT_SECONDS) - total_elapsed,
                float(self.STREAM_IDLE_TIMEOUT_SECONDS) - idle_elapsed,
            )
            try:
                item_type, payload = await asyncio.wait_for(
                    stream_queue.get(),
                    timeout=max(0.1, wait_timeout),
                )
            except asyncio.TimeoutError:
                heartbeat_count += 1
                yield {"type": "ping", "timestamp": int(time.time() * 1000)}
                continue

            last_model_event_at = time.monotonic()

            if item_type == "error":
                raise payload
            if item_type == "done":
                stream_done = True
                continue

            chunk = payload
            last_model_chunk = chunk
            reasoning_text = _extract_reasoning_content(chunk)
            if reasoning_text:
                yield {"type": "thinking", "content": reasoning_text}
            chunk_text = _stringify_message_content(getattr(chunk, "content", ""))
            if first_chunk_delay_ms is None and pre_content_chunk_debug_count < 8:
                additional_kwargs = getattr(chunk, "additional_kwargs", None)
                response_metadata = getattr(chunk, "response_metadata", None)
                logger.info(
                    "[LightweightQAAgent] 首正文前chunk[%d]: content_len=%d reasoning_len=%d "
                    "content_preview=%r reasoning_preview=%r additional_keys=%s response_meta_keys=%s",
                    pre_content_chunk_debug_count,
                    len(chunk_text),
                    len(reasoning_text),
                    _summarize_text(chunk_text, max_length=120),
                    _summarize_text(reasoning_text, max_length=120),
                    (
                        sorted(additional_kwargs.keys())
                        if isinstance(additional_kwargs, dict)
                        else []
                    ),
                    (
                        sorted(response_metadata.keys())
                        if isinstance(response_metadata, dict)
                        else []
                    ),
                )
                pre_content_chunk_debug_count += 1
            if not chunk_text:
                continue
            answer_parts.append(chunk_text)
            chunk_count += 1
            if first_chunk_delay_ms is None:
                now_m = time.monotonic()
                first_chunk_delay_ms = int((now_m - stream_started_at) * 1000)
                first_chunk_from_answer_model_ms = int(
                    (now_m - answer_model_stream_started_at) * 1000
                )
                logger.info(
                    "[LightweightQAAgent] 首个正文 chunk: 自流开始=%dms, 自进入主模型流式=%dms, model=%s",
                    first_chunk_delay_ms,
                    first_chunk_from_answer_model_ms,
                    self._model_name,
                )
            resolved = token_resolver.feed(chunk_text)
            if resolved:
                yield {"type": "chunk", "content": resolved}
    finally:
        # 有界回收：取消可能因上游 aclose 卡住而无法立刻生效，超时后挂 done
        # 回调观察结果，绝不无限 await 一个可能阻塞在满队列 put 上的生产者。
        await reap_task_bounded(
            producer_task,
            timeout=_STREAM_PRODUCER_CLEANUP_TIMEOUT_SECONDS,
        )
        # 断连(GeneratorExit)/超时抛出时,正常路径末尾的 _save_turn 不会执行,
        # 整轮问答连同已消耗的预算一起丢失(用户重连后问题和答案都不在),与
        # 全功能图断线仍留 checkpoint 历史的体验不一致。仅在异常解栈且尚未保存
        # 时,对已累计的部分答案做一次有界、屏蔽的兜底保存;_save_turn 对空答案
        # 自 no-op。正常路径 exc_info 为空,跳过,由下方完整保存处理。
        if sys.exc_info()[0] is not None and not turn_saved:
            partial_answer = "".join(answer_parts)
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(
                    asyncio.shield(
                        self._save_turn(
                            effective_config, question, partial_answer
                        )
                    ),
                    timeout=_LIGHTWEIGHT_PARTIAL_SAVE_TIMEOUT_SECONDS,
                )
            turn_saved = True

    # 刷新流式解析器缓冲区（处理末尾残留的 token 片段）
    await charge_agent_model_response(last_model_chunk)
    flushed = token_resolver.flush()
    if flushed:
        yield {"type": "chunk", "content": flushed}

    answer_text = "".join(answer_parts)
    from backend.application.agent_executors.image_url_registry import (
        inject_missing_captioned_images,
    )

    corrected_answer = inject_missing_captioned_images(answer_text)
    if corrected_answer != answer_text:
        answer_text = corrected_answer
        yield {
            "type": "message.replace",
            "payload": {"content": corrected_answer},
        }
    if not turn_saved:
        await self._save_turn(effective_config, question, answer_text)
        turn_saved = True
    self._trigger_async_compression(effective_config)
    total_ms = int((time.monotonic() - stream_started_at) * 1000)
    yield {
        "type": "event",
        "event": _make_process_event(
            kind="answer",
            title="回答完成",
            detail=f"轻量问答完成：生成 {chunk_count} 个输出片段",
            level="success",
            metadata={
                "chunk_count": chunk_count,
                "total_ms": total_ms,
                "first_chunk_from_stream_ms": first_chunk_delay_ms,
                "first_chunk_from_answer_model_ms": first_chunk_from_answer_model_ms,
                "kb_called": bool(kb_context),
                "web_called": bool(web_context),
            },
        ),
    }
    logger.info(
        "[LightweightQAAgent] 完成: chunks=%d, chars=%d, total=%dms, "
        "first_chunk_from_stream=%sms, first_chunk_from_answer_model=%sms, "
        "heartbeats=%d, kb=%s, web=%s",
        chunk_count,
        len(answer_text),
        total_ms,
        first_chunk_delay_ms,
        first_chunk_from_answer_model_ms,
        heartbeat_count,
        bool(kb_context),
        bool(web_context),
    )
