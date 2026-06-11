"""No-tool writer stage for composing final answers from collected evidence."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WriterStageConfig:
    label: str
    graph: Any
    model_name: str


class EvidenceWriterStage:
    """Stream a no-tool writer model through the standard stream handler."""

    def __init__(self, config: WriterStageConfig):
        self._config = config

    @staticmethod
    def _trim_text(text: str, *, max_chars: int) -> str:
        text = str(text or "").strip()
        if len(text) <= max_chars:
            return text
        return text[-max_chars:]

    def _build_messages(
        self,
        *,
        question: str,
        project_evidence: str,
        retrieved_evidence: str,
    ) -> list[Any]:
        project_evidence = self._trim_text(project_evidence, max_chars=16000)
        retrieved_evidence = self._trim_text(retrieved_evidence, max_chars=12000)

        prompt = f"""
你是撰写智能体。检索智能体已经完成项目数据查询和知识库检索，下面是它保存的证据。
你不能调用任何工具，也不能继续检索；必须只基于证据回答用户问题。

撰写要求：
1. 第一段直接给结论，不要说“我将继续检索”。
2. 项目实测数据、标准知识库依据、超标判断分开写。
3. 能计算的超标项给出：点位/层位、指标、实测值、标准限值、超标数值和超标倍数。
4. 如果标准限值或项目实测值在证据中没有出现，明确写“当前证据未提供”，不要编造。
5. 不要输出服务器路径，不要复述工具调用过程。
6. 不要把思考过程、推理草稿、验证步骤写入正文；正文只输出面向用户的最终答案。

用户问题：
{question}

<groundwater_project_evidence>
{project_evidence}
</groundwater_project_evidence>

<standard_knowledge_evidence>
{retrieved_evidence}
</standard_knowledge_evidence>
""".strip()
        return [
            SystemMessage(content="你是无工具撰写智能体，只能基于给定证据生成最终答案。"),
            HumanMessage(content=prompt),
        ]

    async def _persist_answer(
        self,
        *,
        effective_config: RunnableConfig,
        answer_text: str,
    ) -> None:
        if not answer_text.strip():
            return
        from backend.application.agent_executors.image_url_registry import resolve_image_tokens

        answer_text = resolve_image_tokens(answer_text)
        graph = self._config.graph
        try:
            await asyncio.wait_for(
                graph.aupdate_state(
                    effective_config,
                    {"messages": [AIMessage(content=answer_text)]},
                    as_node="model",
                ),
                timeout=3.0,
            )
        except Exception:
            try:
                await asyncio.wait_for(
                    graph.aupdate_state(
                        effective_config,
                        {"messages": [AIMessage(content=answer_text)]},
                    ),
                    timeout=3.0,
                )
            except Exception:
                logger.warning("[%s] 撰写智能体答案写入历史失败", self._config.label, exc_info=True)

    async def stream(
        self,
        *,
        question: str,
        effective_config: RunnableConfig,
        handler: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        from backend.core.models import create_chat_model

        if not handler.project_evidence_text and not handler.retrieved_evidence_text:
            return

        messages = self._build_messages(
            question=question,
            project_evidence=handler.project_evidence_text,
            retrieved_evidence=handler.retrieved_evidence_text,
        )
        model = create_chat_model(name=self._config.model_name or None, thinking_enabled=False)
        stream = None
        producer_task: asyncio.Task[None] | None = None
        writer_started = False
        last_downstream_event_at = time.monotonic()

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

            stream = model.astream(messages)
            queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

            async def produce_chunks() -> None:
                try:
                    async for chunk in stream:
                        await queue.put(("chunk", chunk))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await queue.put(("error", exc))
                finally:
                    await queue.put(("done", None))

            from backend.application.agent_executors.stream_timeouts import (
                STREAM_HEARTBEAT_INTERVAL_SECONDS,
            )

            producer_task = asyncio.create_task(produce_chunks())
            while True:
                try:
                    item_type, payload = await asyncio.wait_for(
                        queue.get(),
                        timeout=float(STREAM_HEARTBEAT_INTERVAL_SECONDS),
                    )
                except asyncio.TimeoutError:
                    yield {"type": "ping", "timestamp": int(time.time() * 1000)}
                    continue

                if item_type == "done":
                    break
                if item_type == "error":
                    raise payload

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
                    last_downstream_event_at = time.monotonic()
                    yield item

                if (
                    not emitted
                    and time.monotonic() - last_downstream_event_at
                    >= float(STREAM_HEARTBEAT_INTERVAL_SECONDS)
                ):
                    last_downstream_event_at = time.monotonic()
                    yield {"type": "ping", "timestamp": int(time.time() * 1000)}

            visible_answer = "".join(handler.answer_fragments).strip()
            async for item in handler.handle(
                {
                    "event": "on_chat_model_end",
                    "name": "writer_model",
                    "tags": ["writer_stage"],
                    "metadata": {"stage": "writer"},
                    "data": {"output": AIMessage(content=visible_answer)},
                }
            ):
                yield item
            await self._persist_answer(
                effective_config=effective_config,
                answer_text=visible_answer,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[%s] 撰写智能体流式生成失败", self._config.label)
            if writer_started and getattr(handler, "_model_round_open", False):
                handler._model_round_open = False
            yield {"type": "status", "content": "撰写智能体生成失败，系统未能生成最终答案。"}
        finally:
            if producer_task is not None and not producer_task.done():
                producer_task.cancel()
                try:
                    await producer_task
                except asyncio.CancelledError:
                    pass
            if stream is not None and hasattr(stream, "aclose"):
                try:
                    await asyncio.wait_for(stream.aclose(), timeout=1.0)
                except Exception:
                    logger.debug("[%s] 关闭撰写智能体流时忽略异常", self._config.label, exc_info=True)
