"""No-tool writer stage for composing final answers from collected evidence."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
)
from langchain_core.runnables import RunnableConfig

from backend.application.agent_trace import (
    AgentTraceRecorder,
    create_agent_trace_store,
)
from backend.core.agents.context import ContextEngine, ContextPolicy
from backend.core.utils.async_tasks import (
    close_async_iterator_safely,
    reap_task_bounded,
    task_is_cancelling,
)

logger = logging.getLogger(__name__)

_COMPLIANCE_QUESTION = re.compile(
    r"(?:超标|达标|限值|标准值|超标倍数|是否符合|是否满足|水质类别|类别判定)"
)
_STREAM_COALESCE_SECONDS = 0.02
_STREAM_COALESCE_CHARS = 48
_WRITER_QUEUE_MAXSIZE = 8
_LEDGER_EVIDENCE_MAX_ROWS = 8
_LEDGER_EVIDENCE_MAX_CHARS = 2000
try:
    _WRITER_TOTAL_TIMEOUT_SECONDS = max(
        1.0,
        float(os.getenv("WRITER_STAGE_TOTAL_TIMEOUT_SECONDS", "300")),
    )
except ValueError:
    _WRITER_TOTAL_TIMEOUT_SECONDS = 300.0
# 有界回收生产者任务的上限：取消无法立刻生效时也不无限等待。
_WRITER_PRODUCER_CLEANUP_TIMEOUT_SECONDS = 5.0


def _split_evidence_blocks(text: str, *, max_chars: int = 1800) -> list[str]:
    """Split project evidence at line boundaries before claim extraction."""
    blocks: list[str] = []
    current: list[str] = []
    current_size = 0
    for line in (line.strip() for line in text.splitlines()):
        if not line:
            continue
        line_size = len(line) + 1
        if current and current_size + line_size > max_chars:
            blocks.append("\n".join(current))
            current = []
            current_size = 0
        current.append(line)
        current_size += line_size
    if current:
        blocks.append("\n".join(current))
    return blocks


@dataclass(frozen=True)
class WriterStageConfig:
    label: str
    graph: Any
    model_name: str
    request_metadata: dict[str, Any] | None = None


class EvidenceWriterStage:
    """Stream a no-tool writer model through the standard stream handler."""

    def __init__(self, config: WriterStageConfig):
        self._config = config
        policy = ContextPolicy.from_env(config.model_name)
        self._context_engine = ContextEngine.create(
            model_name=config.model_name,
            policy=policy,
            record_top_k=max(36, policy.context_record_top_k),
        )

    @staticmethod
    def _system_prompt() -> str:
        return "你是无工具撰写智能体，只能基于给定证据生成最终答案。"

    @staticmethod
    def _writing_requirements(question: str) -> str:
        if _COMPLIANCE_QUESTION.search(str(question or "")):
            return """
2. 将项目实测数据、适用标准依据和超标/达标判断分开写。
3. 能计算时给出点位或层位、指标、实测值、标准限值、差值和超标倍数；缺少任何输入时明确说明，禁止猜测。
4. 只使用与指标、标准类别和时间版本直接匹配的标准证据。
""".strip()
        return """
2. 围绕用户实际问题组织答案；项目事实、参考资料、分析结论和建议只在相关时分开写，不要输出空洞或不适用的固定章节。
3. 除非用户明确要求合规或超标判断，否则不要套用“标准限值、超标倍数、超标判断”格式。
4. 方法或方案类问题应先说明如何做，再结合项目证据说明当前能做到什么、还缺什么。
5. 同领域但与核心问题无关的资料不能作为结论依据，应明确说明其相关性不足。
""".strip()

    @classmethod
    def _prompt_template(
        cls,
        *,
        question: str,
        evidence_pack: str,
        evidence_limitations: str = "",
        ledger_block: str = "",
    ) -> str:
        limitations = str(evidence_limitations or "").strip()
        limitation_block = (
            f"\n\n<evidence_limitations>\n{limitations}\n</evidence_limitations>"
            if limitations
            else ""
        )
        ledger_block = str(ledger_block or "").strip()
        ledger_html = (
            f"\n\n<evidence_ledger>\n{ledger_block}\n</evidence_ledger>"
            if ledger_block
            else ""
        )
        return f"""
你是撰写智能体。检索智能体已经完成项目数据查询和知识库检索，下面是它保存的证据。
你不能调用任何工具，也不能继续检索；必须只基于证据回答用户问题。

撰写要求：
1. 第一段直接给结论，不要说“我将继续检索”。
{cls._writing_requirements(question)}
6. 如果回答所需的项目实测值或参考依据没有出现，明确写“当前证据未提供”，不要编造。
7. 不要输出服务器路径、图片 URL、MinIO 地址、`/files/` 路径或代理地址；需要插图时只能使用 `[IMAGE:xxx]` token。
8. 不要把思考过程、推理草稿、验证步骤写入正文；正文只输出面向用户的最终答案。
9. 正文引用必须使用证据台账中的数字编号 [n]，编号与台账完全一致，禁止臆造编号。
10. 回答末尾必须输出「参考文档：」列表：每条单独一行，格式为 `- [n] 文献名或规范名，第 x–y 页`；只复制被引用台账行中的具体文献名称与页码，页码必须原样保留、没有页码不得猜测。
11. 禁止使用知识库容器名称作为引用，禁止把多条引用写在同一段落中。

用户问题：
{question}

{evidence_pack}{ledger_html}{limitation_block}
""".strip()

    def _build_evidence_records(
        self,
        *,
        question: str,
        project_evidence: str,
        retrieved_evidence: str,
        evidence_limitations: str = "",
    ) -> list[dict[str, Any]]:
        from backend.application.agent_executors.image_url_registry import (
            register_image_url,
        )
        from backend.application.image_pipeline import (
            replace_image_urls_for_model_context,
        )
        from backend.core.agents.context.ledger import (
            make_evidence_record,
            parse_multikb_text_evidence,
        )

        retrieved_evidence = replace_image_urls_for_model_context(
            retrieved_evidence,
            register_image=register_image_url,
        )
        records = parse_multikb_text_evidence(retrieved_evidence)
        if project_evidence.strip():
            for index, block in enumerate(
                _split_evidence_blocks(project_evidence), start=1
            ):
                block = replace_image_urls_for_model_context(
                    block,
                    register_image=register_image_url,
                )
                records.append(
                    make_evidence_record(
                        source_tool="project_evidence",
                        source_id="project_evidence",
                        chunk_id=f"project-{index}",
                        doc_title="项目实测数据",
                        content=block,
                        category="tool_result",
                        kind="project_data",
                        tags=["project_data"],
                    )
                )
        return [dict(record) for record in records]

    def _build_messages(
        self,
        *,
        question: str,
        project_evidence: str,
        retrieved_evidence: str,
        evidence_limitations: str = "",
        ledger_rows: list[dict[str, Any]] | None = None,
        ledger_evidence: list[dict[str, Any]] | None = None,
    ) -> list[Any]:
        records = self._build_evidence_records(
            question=question,
            project_evidence=project_evidence,
            retrieved_evidence=retrieved_evidence,
            evidence_limitations=evidence_limitations,
        )
        if ledger_evidence:
            records.extend(list(ledger_evidence))
        ledger_rows = [
            dict(row)
            for row in ledger_rows or []
            if isinstance(row, dict) and str(row.get("sid") or "").strip()
        ]
        ledger_block = ""
        compiler = getattr(self._context_engine, "projection_compiler", None)
        if ledger_rows and compiler is not None:
            estimate_messages = [
                HumanMessage(
                    content=self._prompt_template(
                        question=question,
                        evidence_pack="",
                        evidence_limitations=evidence_limitations,
                    )
                )
            ]
            estimate = self._context_engine.compile_request(
                records,
                messages=estimate_messages,
                query=question,
                system_content=self._system_prompt(),
                request_overhead={"writer_stage": True},
            )
            budget_state = dict((estimate.get("pack") or {}).get("budget") or {})
            dynamic_budget = max(
                0,
                int(
                    budget_state.get("dynamic_token_budget")
                    or self._context_engine.ledger.token_budget
                ),
            )
            ledger_block, _ledger_cost = compiler.render_ledger(
                ledger_rows,
                budget_tokens=dynamic_budget,
                dynamic_budget=dynamic_budget,
            )

        base_messages = [
            HumanMessage(
                content=self._prompt_template(
                    question=question,
                    evidence_pack="",
                    evidence_limitations=evidence_limitations,
                    ledger_block=ledger_block,
                )
            )
        ]
        compiled = self._context_engine.compile_request(
            records,
            messages=base_messages,
            query=question,
            system_content=self._system_prompt(),
            request_overhead={"writer_stage": True},
        )
        system_content = str(compiled.get("system_content") or "")
        return [
            SystemMessage(content=system_content),
            *list(compiled.get("messages") or []),
        ]

    def _ledger_rows_for_request(self) -> list[dict[str, Any]]:
        """Return request-stable ledger rows from the live capability ledger."""

        metadata = self._config.request_metadata or {}
        request_id = str(
            metadata.get("_context_scope_id") or metadata.get("_request_id") or ""
        ).strip()
        from backend.application.tools.providers.context_read_tool import (
            get_capability_ledger,
        )

        ledger = get_capability_ledger(request_id or None)
        if ledger is None:
            return []
        rows = ledger.ledger_rows()
        if not rows:
            return []
        ledger.reassign_citation_numbers()
        return ledger.ledger_rows()

    def _ledger_evidence_records(
        self,
        rows: list[dict[str, Any]],
        *,
        max_rows: int = _LEDGER_EVIDENCE_MAX_ROWS,
        max_chars: int = _LEDGER_EVIDENCE_MAX_CHARS,
    ) -> list[dict[str, Any]]:
        """Materialize request-authorized ledger segments for the writer.

        After history compaction the model may fetch evidence through
        context_read instead of a fresh search; the request handler then has
        no retrieval text and the no-tool writer would otherwise be skipped.
        This reads the same request-authorized sids from the object store so a
        final answer can still be produced from the evidence ledger.
        """

        if not rows:
            return []
        metadata = self._config.request_metadata or {}
        request_id = str(
            metadata.get("_context_scope_id") or metadata.get("_request_id") or ""
        ).strip()
        from backend.application.tools.providers.context_read_tool import (
            get_capability_ledger,
        )
        from backend.core.agents.context.artifact_store import (
            context_owner_allowed,
        )
        from backend.core.agents.context.ledger import make_evidence_record

        ledger = get_capability_ledger(request_id or None)
        if ledger is None or ledger.object_store is None:
            return []
        store = ledger.object_store
        records: list[dict[str, Any]] = []
        for row in rows[: max(1, int(max_rows))]:
            sid = str(row.get("sid") or "").strip()
            if not sid:
                continue
            grant = ledger.resolve_sid(sid)
            if grant is None:
                continue
            # 与 context_read 一致的多属主校验:内容寻址对象跨用户/线程共享,
            # sidecar 的 owner_user_id/owner_thread_id 会被最后写者覆盖——
            # 单属主校验会静默跳过先前属主的兜底物化,而该路径是压缩后仅用
            # context_read 的请求产出最终答案的最后防线。read 走请求级缓存,
            # 不产生额外存储往返。
            obj = store.read(grant.oid)
            if obj is None:
                continue
            if not context_owner_allowed(
                dict(obj.get("origin") or {}),
                list(obj.get("owners") or []),
                user_id=ledger.user_id,
                thread_id=ledger.thread_id,
            ):
                continue
            segments = store.read_segments(grant.oid, [sid])
            if not segments:
                continue
            segment = segments[0]
            content = str(segment.get("text") or "").strip()[: max(1, int(max_chars))]
            if not content:
                continue
            records.append(
                dict(
                    make_evidence_record(
                        source_tool="context_object",
                        source_id=sid,
                        chunk_id=sid,
                        doc_title=str(row.get("title") or segment.get("title") or sid),
                        content=content,
                        category="retrieval",
                        kind="retrieval_evidence",
                        score=float(row.get("score") or 0.0),
                        tags=["context_object", "ledger_materialized"],
                    )
                )
            )
        return records

    async def _answer_already_persisted(
        self,
        effective_config: RunnableConfig,
        answer_text: str,
    ) -> bool:
        """读回图状态确认答案是否已落库(仅用于写入超时后的歧义消解)。"""

        graph = self._config.graph
        try:
            state = await asyncio.wait_for(
                graph.aget_state(effective_config),
                timeout=2.0,
            )
        except Exception:
            return False
        values = getattr(state, "values", None) or {}
        messages = values.get("messages") if isinstance(values, dict) else None
        expected = answer_text.strip()
        for message in list(messages or [])[-4:]:
            if (
                isinstance(message, AIMessage)
                and str(message.content or "").strip() == expected
            ):
                return True
        return False

    async def _persist_answer(
        self,
        *,
        effective_config: RunnableConfig,
        answer_text: str,
    ) -> bool:
        """把撰写答案追加进图状态;返回是否确认落库成功。

        返回值决定调用方是否置位 ``mark_answer_persisted_to_state``:失败时
        绝不能置位,否则递归上限/超时路径的兜底 ``aupdate_state`` 会被跳过,
        用户看到的流式答案在刷新后从历史中消失。
        """

        if not answer_text.strip():
            return False
        graph = self._config.graph
        update = {"messages": [AIMessage(content=answer_text)]}
        try:
            await asyncio.wait_for(
                graph.aupdate_state(effective_config, update, as_node="model"),
                timeout=3.0,
            )
            return True
        # Python 3.10 keeps asyncio.TimeoutError distinct from the builtin.
        except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041
            # 超时后底层提交可能仍在后台完成:先读回验证,不盲目重写——
            # add_messages 按新消息 id 追加不去重,盲目重试会把同一答案
            # 追加成第二条 AIMessage。验证未命中才重试一次。
            if await self._answer_already_persisted(effective_config, answer_text):
                return True
            try:
                await asyncio.wait_for(
                    graph.aupdate_state(effective_config, update, as_node="model"),
                    timeout=3.0,
                )
                return True
            except Exception:
                logger.warning(
                    "[%s] 撰写智能体答案写入历史失败(超时重试后)",
                    self._config.label,
                    exc_info=True,
                )
                return False
        except Exception:
            # as_node 不被当前图接受等确定性失败:安全地退回无 as_node 写入。
            try:
                await asyncio.wait_for(
                    graph.aupdate_state(effective_config, update),
                    timeout=3.0,
                )
                return True
            except Exception:
                logger.warning(
                    "[%s] 撰写智能体答案写入历史失败", self._config.label, exc_info=True
                )
                return False

    async def stream(
        self,
        *,
        question: str,
        effective_config: RunnableConfig,
        handler: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        import backend.core.models as core_models

        ledger_rows = self._ledger_rows_for_request()
        # 台账物化会对多达 ~16 个 sidecar 做 store.read/read_segments;当
        # CONTEXT_ARTIFACT_STORE=minio 时这些是同步网络 GET,留在事件循环上
        # 会在 MinIO 抖动时冻结全部会话的 SSE。整体离线到线程(与紧随其后的
        # _build_messages 处理一致)。
        ledger_evidence = await asyncio.to_thread(
            self._ledger_evidence_records, ledger_rows
        )
        if not handler.project_evidence_text and not handler.retrieved_evidence_text:
            if not ledger_evidence:
                return
            logger.info(
                "[%s] 无检索文本，已从证据台账物化 %d 条对象证据用于撰写兜底",
                self._config.label,
                len(ledger_evidence),
            )

        # Tool-calling models sometimes stream a short planning sentence before
        # returning more tool calls.  It is useful live UI context but is not
        # the final answer that should be restored from history.
        pre_writer_answer_length = len("".join(handler.answer_fragments))

        messages = await asyncio.to_thread(
            self._build_messages,
            question=question,
            project_evidence=handler.project_evidence_text,
            retrieved_evidence=handler.retrieved_evidence_text,
            evidence_limitations=str(getattr(handler, "evidence_gap_text", "") or ""),
            ledger_rows=ledger_rows,
            ledger_evidence=ledger_evidence,
        )
        trace = self._trace_recorder()
        trace.record_event(
            sequence=0,
            event_type="writer.input",
            payload={
                "question_preview": _preview_text(question),
                "message_count": len(messages),
                "input_chars": sum(
                    len(str(getattr(message, "content", "") or ""))
                    for message in messages
                ),
                "messages_preview": _preview_messages(messages),
            },
        )
        # Resolve through the public module at call time so deployments and
        # tests retain their supported model-factory injection point.
        model = await asyncio.to_thread(
            core_models.create_chat_model,
            name=self._config.model_name or None,
            thinking_enabled=False,
        )
        stream = None
        producer_task: asyncio.Task[None] | None = None
        writer_started = False
        last_downstream_event_at = time.monotonic()
        aggregated_chunk: AIMessageChunk | None = None
        input_chunk_count = 0
        emitted_event_count = 0

        try:
            async for item in handler.handle(
                {
                    "event": "on_chat_model_start",
                    "name": "writer_model",
                    "tags": ["writer_stage"],
                    "metadata": {"stage": "writer"},
                    "data": {"input": messages},
                }
            ):
                yield item
            writer_started = True
            yield {"type": "status", "content": "撰写智能体正在生成最终答案..."}

            stream = model.astream(messages)
            queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(
                maxsize=_WRITER_QUEUE_MAXSIZE
            )

            async def produce_chunks() -> None:
                try:
                    async for chunk in stream:
                        await queue.put(("chunk", chunk))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not task_is_cancelling(asyncio.current_task()):
                        await queue.put(("error", exc))
                finally:
                    # 取消时绝不再向 8 格队列写：满队列上的 put 会永久阻塞、泄漏撰写
                    # 模型流；取消信号已被消费，不会再有唤醒（与 F1 同一死锁模式）。
                    if not task_is_cancelling(asyncio.current_task()):
                        await queue.put(("done", None))

            from backend.application.agent_executors.stream_timeouts import (
                STREAM_HEARTBEAT_INTERVAL_SECONDS,
            )

            producer_task = asyncio.create_task(produce_chunks())
            writer_deadline = time.monotonic() + _WRITER_TOTAL_TIMEOUT_SECONDS
            pending_queue_item: tuple[str, Any] | None = None
            while True:
                if time.monotonic() >= writer_deadline:
                    raise TimeoutError(
                        f"writer stage exceeded {_WRITER_TOTAL_TIMEOUT_SECONDS:.0f}s"
                    )
                if pending_queue_item is not None:
                    item_type, payload = pending_queue_item
                    pending_queue_item = None
                else:
                    try:
                        item_type, payload = await asyncio.wait_for(
                            queue.get(),
                            timeout=min(
                                float(STREAM_HEARTBEAT_INTERVAL_SECONDS),
                                max(0.1, writer_deadline - time.monotonic()),
                            ),
                        )
                    # Python 3.10 keeps asyncio.TimeoutError distinct from the
                    # builtin TimeoutError used by newer runtimes.
                    except asyncio.TimeoutError:  # noqa: UP041
                        yield {"type": "ping", "timestamp": int(time.time() * 1000)}
                        continue

                if item_type == "done":
                    break
                if item_type == "error":
                    raise payload

                raw_chunk_count = 1
                if isinstance(payload, AIMessageChunk):
                    batch_chars = len(str(payload.content or ""))
                    deadline = time.monotonic() + _STREAM_COALESCE_SECONDS
                    while batch_chars < _STREAM_COALESCE_CHARS:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            next_type, next_payload = await asyncio.wait_for(
                                queue.get(),
                                timeout=remaining,
                            )
                        except asyncio.TimeoutError:  # noqa: UP041
                            break
                        if next_type != "chunk" or not isinstance(
                            next_payload, AIMessageChunk
                        ):
                            pending_queue_item = (next_type, next_payload)
                            break
                        payload = payload + next_payload
                        raw_chunk_count += 1
                        batch_chars += len(str(next_payload.content or ""))

                input_chunk_count += raw_chunk_count
                if isinstance(payload, AIMessageChunk):
                    aggregated_chunk = (
                        payload
                        if aggregated_chunk is None
                        else aggregated_chunk + payload
                    )

                emitted = False
                async for item in handler.handle(
                    {
                        "event": "on_chat_model_stream",
                        "name": "writer_model",
                        "tags": ["writer_stage"],
                        "metadata": {"stage": "writer"},
                        "data": {"chunk": payload},
                    }
                ):
                    emitted = True
                    emitted_event_count += 1
                    last_downstream_event_at = time.monotonic()
                    yield item

                if input_chunk_count <= 8:
                    content = getattr(payload, "content", "")
                    additional_kwargs = getattr(payload, "additional_kwargs", None)
                    logger.info(
                        "[%s] 撰写流 chunk[%d]: content_type=%s content_len=%d "
                        "additional_keys=%s downstream_events=%d",
                        self._config.label,
                        input_chunk_count,
                        type(content).__name__,
                        len(str(content or "")),
                        (
                            sorted(additional_kwargs.keys())
                            if isinstance(additional_kwargs, dict)
                            else []
                        ),
                        emitted_event_count,
                    )

                if not emitted and time.monotonic() - last_downstream_event_at >= float(
                    STREAM_HEARTBEAT_INTERVAL_SECONDS
                ):
                    last_downstream_event_at = time.monotonic()
                    yield {"type": "ping", "timestamp": int(time.time() * 1000)}

            visible_answer = "".join(handler.answer_fragments).strip()
            final_output: AIMessage | AIMessageChunk
            if aggregated_chunk is not None:
                final_output = aggregated_chunk
            else:
                final_output = AIMessage(content=visible_answer)
            async for item in handler.handle(
                {
                    "event": "on_chat_model_end",
                    "name": "writer_model",
                    "tags": ["writer_stage"],
                    "metadata": {"stage": "writer"},
                    "data": {"output": final_output},
                }
            ):
                yield item
            visible_answer = "".join(handler.answer_fragments)[
                pre_writer_answer_length:
            ].strip()
            from backend.application.agent_executors.image_url_registry import (
                inject_missing_captioned_images,
            )

            corrected_answer = inject_missing_captioned_images(visible_answer)
            if pre_writer_answer_length or corrected_answer != visible_answer:
                handler.answer_fragments = [corrected_answer]
                visible_answer = corrected_answer.strip()
                yield {
                    "type": "message.replace",
                    "payload": {"content": corrected_answer},
                }
            logger.info(
                "[%s] 撰写智能体流结束: input_chunks=%d downstream_events=%d visible_chars=%d",
                self._config.label,
                input_chunk_count,
                emitted_event_count,
                len(visible_answer),
            )
            trace.record_event(
                sequence=1,
                event_type="writer.output",
                payload={
                    "status": "completed",
                    "input_chunk_count": input_chunk_count,
                    "emitted_event_count": emitted_event_count,
                    "visible_chars": len(visible_answer),
                    "answer_preview": _preview_text(visible_answer),
                },
            )
            answer_persisted = await self._persist_answer(
                effective_config=effective_config,
                answer_text=visible_answer,
            )
            # 仅在确认落库成功后告知 handler,阻止 impl 的兜底 aupdate_state
            # 二次追加;落库失败时保留兜底写回作为答案进历史的最后防线。
            if answer_persisted:
                mark_persisted = getattr(
                    handler, "mark_answer_persisted_to_state", None
                )
                if callable(mark_persisted):
                    mark_persisted()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[%s] 撰写智能体流式生成失败", self._config.label)
            if writer_started and handler.model_round_open:
                handler.mark_model_round_closed()
            mark_failed = getattr(handler, "mark_writer_stage_failed", None)
            if callable(mark_failed):
                mark_failed()
            trace.record_event(
                sequence=1,
                event_type="writer.output",
                payload={
                    "status": "failed",
                    "input_chunk_count": input_chunk_count,
                    "emitted_event_count": emitted_event_count,
                },
            )
            yield {
                "type": "status",
                "content": "撰写智能体生成失败，系统未能生成最终答案。",
            }
        finally:
            # 有界回收：绝不无限 await 一个可能阻塞在满队列 put 上的生产者。
            await reap_task_bounded(
                producer_task,
                timeout=_WRITER_PRODUCER_CLEANUP_TIMEOUT_SECONDS,
            )
            if stream is not None and hasattr(stream, "aclose"):
                await close_async_iterator_safely(
                    stream,
                    active_read_task=(
                        producer_task
                        if producer_task is not None and not producer_task.done()
                        else None
                    ),
                    timeout=1.0,
                )

    def _trace_recorder(self) -> AgentTraceRecorder:
        metadata = dict(self._config.request_metadata or {})
        run_id = str(
            metadata.get("_trace_run_id") or metadata.get("_request_id") or ""
        ).strip()
        trace_root = metadata.get("agent_trace_root")
        return AgentTraceRecorder(
            run_id=run_id or "writer-stage",
            workflow_name=str(
                metadata.get("_workflow_name")
                or metadata.get("coordination_workflow")
                or "writer"
            ),
            enabled=bool(metadata.get("_trace_enabled") and run_id),
            store=(
                create_agent_trace_store(trace_root, backend="jsonl")
                if trace_root
                else None
            ),
        )


def _preview_text(value: object, *, max_chars: int = 1200) -> str:
    text = str(value or "")
    return text if len(text) <= max_chars else text[:max_chars] + "...[truncated]"


def _preview_messages(
    messages: list[Any], *, max_chars: int = 800
) -> list[dict[str, Any]]:
    preview: list[dict[str, Any]] = []
    for message in messages:
        preview.append(
            {
                "type": type(message).__name__,
                "content_preview": _preview_text(
                    getattr(message, "content", ""),
                    max_chars=max_chars,
                ),
            }
        )
    return preview
